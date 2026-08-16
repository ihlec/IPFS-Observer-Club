package limit

import (
	"testing"
	"time"
)

func TestAllowCapsPerWindow(t *testing.T) {
	l := New(3, time.Minute)
	for i := 0; i < 3; i++ {
		if !l.Allow("p1") {
			t.Fatalf("allowed %d should pass", i)
		}
	}
	if l.Allow("p1") {
		t.Fatal("4th message should be limited")
	}
	if !l.Allow("p2") {
		t.Fatal("other publisher should pass")
	}
}

func TestDuplicate(t *testing.T) {
	l := New(10, time.Minute)
	if l.Duplicate("aaa") {
		t.Fatal("first sighting is not a duplicate")
	}
	if !l.Duplicate("aaa") {
		t.Fatal("second sighting is a duplicate")
	}
	if l.Duplicate("bbb") {
		t.Fatal("different hash is new")
	}
}

func TestWindowExpiry(t *testing.T) {
	l := New(1, 20*time.Millisecond)
	if !l.Allow("p") {
		t.Fatal("first")
	}
	if l.Allow("p") {
		t.Fatal("capped")
	}
	time.Sleep(30 * time.Millisecond)
	if !l.Allow("p") {
		t.Fatal("after window should pass")
	}
}
