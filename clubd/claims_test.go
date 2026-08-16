package main

import (
	"testing"
	"time"
)

func TestClaimTrackerOneLivePerPeer(t *testing.T) {
	tr := newClaimTracker()
	until := time.Now().Add(time.Minute)
	if !tr.Allow("p1", "cid-a", until) {
		t.Fatal("first claim should pass")
	}
	if tr.Allow("p1", "cid-b", until) {
		t.Fatal("second distinct claim should be rejected")
	}
	if !tr.Allow("p1", "cid-a", until) {
		t.Fatal("refresh of the same CID should pass")
	}
	if !tr.Allow("p2", "cid-c", until) {
		t.Fatal("other publisher should pass")
	}
}

func TestClaimTrackerClearAndExpiry(t *testing.T) {
	tr := newClaimTracker()
	until := time.Now().Add(40 * time.Millisecond)
	if !tr.Allow("p1", "cid-a", until) {
		t.Fatal("first")
	}
	tr.Clear("p1", "cid-a")
	if !tr.Allow("p1", "cid-b", time.Now().Add(time.Minute)) {
		t.Fatal("after clear should pass")
	}
	tr = newClaimTracker()
	if !tr.Allow("p1", "cid-a", time.Now().Add(25*time.Millisecond)) {
		t.Fatal("short lease")
	}
	time.Sleep(40 * time.Millisecond)
	if !tr.Allow("p1", "cid-b", time.Now().Add(time.Minute)) {
		t.Fatal("expired lease should free the slot")
	}
}
