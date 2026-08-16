// Package limit rate-limits and deduplicates club gossip per publisher.
package limit

import (
	"sync"
	"time"
)

type Limiter struct {
	mu      sync.Mutex
	window  time.Duration
	max     int
	hits    map[string][]time.Time
	hashes  map[string]time.Time
	hashCap int
}

func New(maxPerWindow int, window time.Duration) *Limiter {
	if maxPerWindow <= 0 {
		maxPerWindow = 60
	}
	if window <= 0 {
		window = time.Minute
	}
	return &Limiter{
		window:  window,
		max:     maxPerWindow,
		hits:    map[string][]time.Time{},
		hashes:  map[string]time.Time{},
		hashCap: 20000,
	}
}

// Duplicate reports whether payloadHash was already accepted recently.
func (l *Limiter) Duplicate(payloadHash string) bool {
	if payloadHash == "" {
		return false
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, ok := l.hashes[payloadHash]; ok {
		return true
	}
	now := time.Now()
	if len(l.hashes) >= l.hashCap {
		cutoff := now.Add(-l.window)
		for h, t := range l.hashes {
			if t.Before(cutoff) {
				delete(l.hashes, h)
			}
		}
		if len(l.hashes) >= l.hashCap {
			l.hashes = map[string]time.Time{}
		}
	}
	l.hashes[payloadHash] = now
	return false
}

// Allow reports whether publisher may emit one more message in the window.
func (l *Limiter) Allow(publisher string) bool {
	if publisher == "" {
		return false
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	cutoff := now.Add(-l.window)
	cur := l.hits[publisher]
	kept := cur[:0]
	for _, t := range cur {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	if cap(kept) == 0 {
		kept = []time.Time{}
	}
	if len(kept) >= l.max {
		l.hits[publisher] = kept
		return false
	}
	l.hits[publisher] = append(kept, now)
	return true
}
