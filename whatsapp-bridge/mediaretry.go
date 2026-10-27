package main

// Media retry: recover attachments WhatsApp has already deleted from its CDN.
//
// The CDN keeps media for about a month; after that a download answers 403/410.
// WhatsApp Web/Desktop still open such files because they ask the account's
// phone to upload them again. This file does the same thing: it sends a media
// retry receipt to our own phone, waits for the *events.MediaRetry answer,
// decrypts the new direct path, and writes the fresh CDN URL back into the
// messages table. The caller (the MCP's download_media) then downloads the file
// as usual. It only works while the phone still has the file and is online.

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

const mediaRetryTimeout = 45 * time.Second

var (
	mediaRetryMu      sync.Mutex
	mediaRetryWaiters = map[string]chan *events.MediaRetry{}
)

// deliverMediaRetry hands a phone's answer to the HTTP request waiting for it.
func deliverMediaRetry(evt *events.MediaRetry) {
	mediaRetryMu.Lock()
	ch := mediaRetryWaiters[evt.MessageID]
	mediaRetryMu.Unlock()
	if ch != nil {
		select {
		case ch <- evt:
		default:
		}
	}
}

type MediaRetryResponse struct {
	Success bool   `json:"success"`
	Code    string `json:"code,omitempty"`
	Message string `json:"message"`
	URL     string `json:"url,omitempty"`
}

// senderJID rebuilds the participant JID. The messages table keeps only the user
// part; a user that appears in the LID map is a LID, anything else a phone JID.
func senderJID(ctx context.Context, client *whatsmeow.Client, user string) types.JID {
	lid := types.NewJID(user, types.HiddenUserServer)
	if pn, err := client.Store.LIDs.GetPNForLID(ctx, lid); err == nil && !pn.IsEmpty() {
		return lid
	}
	return types.NewJID(user, types.DefaultUserServer)
}

func requestMediaRetry(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string) MediaRetryResponse {
	var mediaKey []byte
	var sender string
	var isFromMe bool
	err := messageStore.db.QueryRow(
		"SELECT media_key, sender, is_from_me FROM messages WHERE id = ? AND chat_jid = ?",
		messageID, chatJID,
	).Scan(&mediaKey, &sender, &isFromMe)
	if errors.Is(err, sql.ErrNoRows) {
		return MediaRetryResponse{Code: "not_found", Message: "message not found"}
	} else if err != nil {
		return MediaRetryResponse{Code: "error", Message: err.Error()}
	}
	if len(mediaKey) == 0 {
		return MediaRetryResponse{Code: "incomplete_media_info", Message: "no media key stored"}
	}
	if !client.IsLoggedIn() {
		return MediaRetryResponse{Code: "not_logged_in", Message: "WhatsApp session is not logged in"}
	}

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return MediaRetryResponse{Code: "bad_request", Message: err.Error()}
	}
	ctx, cancel := context.WithTimeout(context.Background(), mediaRetryTimeout)
	defer cancel()

	info := &types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     chat,
			IsFromMe: isFromMe,
			IsGroup:  chat.Server == types.GroupServer,
		},
		ID: messageID,
	}
	if info.IsGroup {
		if isFromMe && client.Store.ID != nil {
			info.Sender = client.Store.ID.ToNonAD()
		} else {
			info.Sender = senderJID(ctx, client, sender)
		}
	}

	ch := make(chan *events.MediaRetry, 1)
	mediaRetryMu.Lock()
	mediaRetryWaiters[messageID] = ch
	mediaRetryMu.Unlock()
	defer func() {
		mediaRetryMu.Lock()
		delete(mediaRetryWaiters, messageID)
		mediaRetryMu.Unlock()
	}()

	if err := client.SendMediaRetryReceipt(ctx, info, mediaKey); err != nil {
		if errors.Is(err, whatsmeow.ErrNotLoggedIn) {
			return MediaRetryResponse{Code: "not_logged_in", Message: err.Error()}
		}
		return MediaRetryResponse{Code: "error", Message: fmt.Sprintf("send retry receipt: %v", err)}
	}

	var evt *events.MediaRetry
	select {
	case evt = <-ch:
	case <-ctx.Done():
		return MediaRetryResponse{Code: "phone_timeout", Message: "the phone did not answer (offline?)"}
	}

	notif, err := whatsmeow.DecryptMediaRetryNotification(evt, mediaKey)
	if errors.Is(err, whatsmeow.ErrMediaNotAvailableOnPhone) {
		return MediaRetryResponse{Code: "not_on_phone", Message: "the phone no longer has this file"}
	} else if err != nil {
		return MediaRetryResponse{Code: "error", Message: err.Error()}
	}
	switch notif.GetResult() {
	case waMmsRetry.MediaRetryNotification_SUCCESS:
	case waMmsRetry.MediaRetryNotification_NOT_FOUND:
		return MediaRetryResponse{Code: "not_on_phone", Message: "the phone no longer has this file"}
	default:
		return MediaRetryResponse{Code: "error", Message: "phone answered " + notif.GetResult().String()}
	}

	url := "https://mmg.whatsapp.net" + notif.GetDirectPath() + "&mms3=true"
	if _, err := messageStore.db.Exec(
		"UPDATE messages SET url = ? WHERE id = ? AND chat_jid = ?", url, messageID, chatJID,
	); err != nil {
		return MediaRetryResponse{Code: "error", Message: fmt.Sprintf("store new url: %v", err)}
	}
	fmt.Printf("Media retry succeeded for %s in %s\n", messageID, chatJID)
	return MediaRetryResponse{Success: true, Message: "phone re-uploaded the media", URL: url}
}

func registerMediaRetryHandler(client *whatsmeow.Client, messageStore *MessageStore) {
	http.HandleFunc("/api/media_retry", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "message_id and chat_jid are required", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(requestMediaRetry(client, messageStore, req.MessageID, req.ChatJID))
	})
}
