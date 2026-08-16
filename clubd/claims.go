package main

import (
	"encoding/json"
	"sync"
	"time"
)

// ClaimTracker enforces the protocol rule: at most one in-flight claim per peer.
type ClaimTracker struct {
	mu   sync.Mutex
	live map[string]claimSlot
}

type claimSlot struct {
	cid   string
	until time.Time
}

func newClaimTracker() *ClaimTracker {
	return &ClaimTracker{live: map[string]claimSlot{}}
}

func (t *ClaimTracker) Allow(publisher, cid string, until time.Time) bool {
	if t == nil || publisher == "" || cid == "" {
		return true
	}
	if until.IsZero() || !until.After(time.Now()) {
		return false
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	now := time.Now()
	cur, ok := t.live[publisher]
	if ok && cur.until.After(now) && cur.cid != cid {
		return false
	}
	t.live[publisher] = claimSlot{cid: cid, until: until}
	return true
}

func (t *ClaimTracker) Clear(publisher, cid string) {
	if t == nil || publisher == "" {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	cur, ok := t.live[publisher]
	if ok && (cid == "" || cur.cid == cid) {
		delete(t.live, publisher)
	}
}

func untilOf(m map[string]interface{}) time.Time {
	switch v := m["until"].(type) {
	case json.Number:
		n, err := v.Int64()
		if err != nil {
			return time.Time{}
		}
		return time.Unix(n, 0)
	case float64:
		return time.Unix(int64(v), 0)
	case int64:
		return time.Unix(v, 0)
	case int:
		return time.Unix(int64(v), 0)
	default:
		return time.Time{}
	}
}

func stringOf(m map[string]interface{}, key string) string {
	v, _ := m[key].(string)
	return v
}
