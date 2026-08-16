package main

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/libp2p/go-libp2p/core/protocol"
)

var clubIDRe = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$`)

func normalizeClubID(id string) (string, error) {
	id = strings.ToLower(strings.TrimSpace(id))
	if !clubIDRe.MatchString(id) {
		return "", fmt.Errorf("invalid club id %q (use 1-32 chars of [a-z0-9-])", id)
	}
	return id, nil
}

func validateClubID(id string) error {
	_, err := normalizeClubID(id)
	return err
}

func topicPrefixFor(clubID string) string {
	return fmt.Sprintf("ipfs-observer-club/v1/%s/", clubID)
}

func mdnsServiceFor(clubID string) string {
	return fmt.Sprintf("ipfs-observer-club-%s", clubID)
}

func snapshotProtoFor(clubID string) protocol.ID {
	return protocol.ID(fmt.Sprintf("/ipfs-observer-club/v1/%s/snapshot", clubID))
}
