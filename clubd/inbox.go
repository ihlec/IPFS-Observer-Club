package main

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type inboxFile struct {
	path string
	name string
	mod  time.Time
	size int64
}

func listJSONL(dir string) ([]inboxFile, error) {
	ents, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	out := []inboxFile{}
	for _, e := range ents {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".jsonl") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, inboxFile{
			path: filepath.Join(dir, e.Name()),
			name: e.Name(),
			mod:  info.ModTime(),
			size: info.Size(),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].name == out[j].name {
			return out[i].mod.Before(out[j].mod)
		}
		return out[i].name < out[j].name
	})
	return out, nil
}

func pruneInboxDir(dir string, keepDays int, maxBytes int64) error {
	if keepDays <= 0 {
		keepDays = 7
	}
	if maxBytes <= 0 {
		maxBytes = 64 << 20
	}
	files, err := listJSONL(dir)
	if err != nil {
		return err
	}
	cutoff := time.Now().UTC().AddDate(0, 0, -keepDays)
	today := time.Now().UTC().Format("2006-01-02") + ".jsonl"
	var total int64
	for _, f := range files {
		total += f.size
	}
	for _, f := range files {
		if f.name == today {
			continue
		}
		stale := f.mod.Before(cutoff) || strings.TrimSuffix(f.name, ".jsonl") < cutoff.Format("2006-01-02")
		over := total > maxBytes
		if !stale && !over {
			continue
		}
		if err := os.Remove(f.path); err != nil && !os.IsNotExist(err) {
			return err
		}
		total -= f.size
	}
	return nil
}
