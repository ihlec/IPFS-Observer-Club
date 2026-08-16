package main

import (
	"context"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
)

const maxSnapshotBytes = 32 << 20

func (d *daemon) setupSync(ctx context.Context) {
	d.host.SetStreamHandler(d.snapshotProto, d.handleSnapshotStream)
	d.host.Network().Notify(&network.NotifyBundle{
		ConnectedF: func(_ network.Network, c network.Conn) {
			pid := c.RemotePeer()
			if pid == d.host.ID() {
				return
			}
			go d.requestSnapshot(ctx, pid)
		},
	})
	go d.syncLoop(ctx)
}

func (d *daemon) handleSnapshotStream(s network.Stream) {
	defer s.Close()
	remote := s.Conn().RemotePeer()
	if !d.snapshotServeAllow(remote) {
		return
	}
	if d.snapshotURL == "" {
		return
	}
	reqCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, d.snapshotURL, nil)
	if err != nil {
		return
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Printf("snapshot serve %s: %v", remote, err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return
	}
	_, _ = io.Copy(s, io.LimitReader(resp.Body, maxSnapshotBytes))
}

func (d *daemon) requestSnapshot(ctx context.Context, pid peer.ID) {
	if !d.snapshotPullAllow(pid) {
		return
	}
	sctx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	s, err := d.host.NewStream(sctx, pid, d.snapshotProto)
	if err != nil {
		log.Printf("snapshot dial %s: %v", pid, err)
		return
	}
	defer s.Close()
	_ = s.SetDeadline(time.Now().Add(45 * time.Second))
	raw, err := io.ReadAll(io.LimitReader(s, maxSnapshotBytes))
	if err != nil && len(raw) == 0 {
		log.Printf("snapshot read %s: %v", pid, err)
		return
	}
	n := 0
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if err := d.acceptGossip([]byte(line), false); err != nil {
			continue
		}
		n++
	}
	if n > 0 {
		log.Printf("snapshot from %s: %d messages", pid, n)
	}
}

func (d *daemon) syncLoop(ctx context.Context) {
	if d.snapshotEvery <= 0 {
		d.snapshotEvery = 2 * time.Minute
	}
	t := time.NewTicker(d.snapshotEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			connectBootstrap(ctx, d.host, d.bootstrap)
			for _, pid := range d.host.Network().Peers() {
				go d.requestSnapshot(ctx, pid)
			}
		}
	}
}

func (d *daemon) snapshotServeAllow(pid peer.ID) bool {
	d.syncMu.Lock()
	defer d.syncMu.Unlock()
	now := time.Now()
	if t, ok := d.servedAt[pid]; ok && now.Sub(t) < time.Minute {
		return false
	}
	d.servedAt[pid] = now
	return true
}

func (d *daemon) snapshotPullAllow(pid peer.ID) bool {
	d.syncMu.Lock()
	defer d.syncMu.Unlock()
	now := time.Now()
	if t, ok := d.pulledAt[pid]; ok && now.Sub(t) < 10*time.Minute {
		return false
	}
	d.pulledAt[pid] = now
	return true
}
