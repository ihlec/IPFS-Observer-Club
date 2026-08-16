package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestPruneInboxDeletesStaleAndCapsSize(t *testing.T) {
	dir := t.TempDir()
	old := filepath.Join(dir, "2000-01-01.jsonl")
	if err := os.WriteFile(old, []byte("aaaa\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	oldTime := time.Now().Add(-20 * 24 * time.Hour)
	_ = os.Chtimes(old, oldTime, oldTime)
	today := filepath.Join(dir, time.Now().UTC().Format("2006-01-02")+".jsonl")
	if err := os.WriteFile(today, []byte("bbbb\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := pruneInboxDir(dir, 7, 64<<20); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(old); !os.IsNotExist(err) {
		t.Fatal("expected stale file removed")
	}
	if _, err := os.Stat(today); err != nil {
		t.Fatal("today's inbox should remain")
	}
}
