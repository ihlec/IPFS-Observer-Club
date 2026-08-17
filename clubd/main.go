// Command clubd is the libp2p gossip + signing daemon for IPFS Observer Club.
//
// It is intentionally small: verify/sign canonical JSON, gossip it, append
// verified messages to a JSONL inbox that the Python observer ingests.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/libp2p/go-libp2p"
	pubsub "github.com/libp2p/go-libp2p-pubsub"
	"github.com/libp2p/go-libp2p/core/crypto"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/core/protocol"
	"github.com/libp2p/go-libp2p/p2p/discovery/mdns"
	"github.com/multiformats/go-multiaddr"

	"github.com/cornelius/ipfs-observer-club/clubd/internal/canon"
	"github.com/cornelius/ipfs-observer-club/clubd/internal/limit"
)

var topics = []string{"claim", "skip", "classify", "alias", "report"}

type daemon struct {
	host          host.Host
	priv          crypto.PrivKey
	ps            *pubsub.PubSub
	joined        map[string]*pubsub.Topic
	inbox         string
	inboxMaxBytes int64
	inboxKeepDays int
	limiter       *limit.Limiter
	claims        *ClaimTracker
	mu            sync.Mutex
	syncMu        sync.Mutex
	servedAt      map[peer.ID]time.Time
	pulledAt      map[peer.ID]time.Time
	snapshotURL   string
	snapshotEvery time.Duration
	snapshotProto protocol.ID
	bootstrap     []string
	clubID        string
}

func main() {
	var (
		clubID        = flag.String("club", "academic", "club id (gossip namespace)")
		port          = flag.Int("port", 4713, "libp2p TCP/QUIC listen port")
		api           = flag.String("api", "127.0.0.1:8003", "local HTTP API bind")
		identity      = flag.String("identity", "data/identity.key", "libp2p private key path")
		inbox         = flag.String("inbox", "data/inbox", "JSONL inbox directory")
		bootstrap     = flag.String("bootstrap", "", "comma-separated multiaddrs")
		snapshotURL   = flag.String("snapshot-url", "http://127.0.0.1:8002/api/snapshot", "local observer snapshot URL")
		rate          = flag.Int("rate", 60, "max verified gossip messages per publisher per minute")
		inboxMaxBytes = flag.Int64("inbox-max-bytes", 64<<20, "inbox size cap")
		inboxKeepDays = flag.Int("inbox-keep-days", 7, "delete older jsonl files")
		enableMDNS    = flag.Bool("mdns", true, "discover other Observers on the LAN")
	)
	flag.Parse()
	normalized, err := normalizeClubID(*clubID)
	if err != nil {
		log.Fatal(err)
	}
	*clubID = normalized
	topicPrefix := topicPrefixFor(*clubID)
	mdnsName := mdnsServiceFor(*clubID)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	priv, err := loadOrCreateKey(*identity)
	if err != nil {
		log.Fatalf("identity: %v", err)
	}

	h, err := libp2p.New(
		libp2p.Identity(priv),
		libp2p.ListenAddrStrings(
			fmt.Sprintf("/ip4/0.0.0.0/tcp/%d", *port),
			fmt.Sprintf("/ip4/0.0.0.0/udp/%d/quic-v1", *port),
		),
		libp2p.UserAgent("ipfs-observer-club/0.1"),
	)
	if err != nil {
		log.Fatalf("libp2p: %v", err)
	}
	defer h.Close()
	log.Printf("peer id %s", h.ID())
	for _, a := range h.Addrs() {
		log.Printf("listen %s/p2p/%s", a, h.ID())
	}

	ps, err := pubsub.NewGossipSub(ctx, h)
	if err != nil {
		log.Fatalf("gossipsub: %v", err)
	}

	d := &daemon{
		host:          h,
		priv:          priv,
		ps:            ps,
		joined:        map[string]*pubsub.Topic{},
		inbox:         *inbox,
		inboxMaxBytes: *inboxMaxBytes,
		inboxKeepDays: *inboxKeepDays,
		limiter:       limit.New(*rate, time.Minute),
		claims:        newClaimTracker(),
		servedAt:      map[peer.ID]time.Time{},
		pulledAt:      map[peer.ID]time.Time{},
		snapshotURL:   *snapshotURL,
		snapshotEvery: 2 * time.Minute,
		snapshotProto: snapshotProtoFor(*clubID),
		bootstrap:     splitBootstrap(*bootstrap),
		clubID:        *clubID,
	}
	if err := os.MkdirAll(*inbox, 0o755); err != nil {
		log.Fatalf("inbox: %v", err)
	}
	for _, name := range topics {
		t, err := ps.Join(topicPrefix + name)
		if err != nil {
			log.Fatalf("join %s: %v", name, err)
		}
		d.joined[name] = t
		sub, err := t.Subscribe()
		if err != nil {
			log.Fatalf("sub %s: %v", name, err)
		}
		go d.consume(ctx, sub)
	}

	log.Printf("club %s topics %s{claim,skip,classify,alias,report}", *clubID, topicPrefix)
	connectBootstrap(ctx, h, d.bootstrapPeers())
	if *enableMDNS {
		svc := mdns.NewMdnsService(h, mdnsName, &mdnsNotifee{h: h})
		if err := svc.Start(); err != nil {
			log.Printf("mdns: %v", err)
		} else {
			defer svc.Close()
			log.Printf("mdns enabled (service %s)", mdnsName)
		}
	}
	d.setupSync(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/id", d.handleID)
	mux.HandleFunc("/v1/publish", d.handlePublish)
	mux.HandleFunc("/v1/peers", d.handlePeers)
	mux.HandleFunc("/v1/bootstrap", d.handleBootstrap)

	srv := &http.Server{Addr: *api, Handler: mux}
	go func() {
		log.Printf("api http://%s", *api)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("api: %v", err)
		}
	}()
	<-ctx.Done()
	_ = srv.Shutdown(context.Background())
}

func (d *daemon) handleID(w http.ResponseWriter, r *http.Request) {
	addrs := []string{}
	for _, a := range d.host.Addrs() {
		addrs = append(addrs, fmt.Sprintf("%s/p2p/%s", a, d.host.ID()))
	}
	writeJSON(w, map[string]interface{}{
		"peer_id": d.host.ID().String(),
		"club":    d.clubID,
		"addrs":   addrs,
	})
}

func (d *daemon) handlePeers(w http.ResponseWriter, r *http.Request) {
	ps := d.host.Network().Peers()
	ids := make([]string, 0, len(ps))
	for _, p := range ps {
		ids = append(ids, p.String())
	}
	writeJSON(w, map[string]interface{}{"peers": ids})
}

type bootstrapBody struct {
	Peers []string `json:"peers"`
}

type bootstrapDial struct {
	Addr  string `json:"addr"`
	Peer  string `json:"peer,omitempty"`
	Error string `json:"error,omitempty"`
}

func (d *daemon) handleBootstrap(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet, http.MethodHead:
		writeJSON(w, map[string]interface{}{"peers": d.bootstrapPeers()})
	case http.MethodPost:
		raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<16))
		if err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		var body bootstrapBody
		if len(bytes.TrimSpace(raw)) > 0 {
			if err := json.Unmarshal(raw, &body); err != nil {
				http.Error(w, "invalid json", 400)
				return
			}
		}
		peers, err := normalizeBootstrapPeers(body.Peers)
		if err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		d.setBootstrapPeers(peers)
		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()
		dial := connectBootstrap(ctx, d.host, peers)
		connected := make([]string, 0)
		for _, p := range d.host.Network().Peers() {
			connected = append(connected, p.String())
		}
		writeJSON(w, map[string]interface{}{
			"peers":     peers,
			"connected": connected,
			"dial":      dial,
		})
	default:
		http.Error(w, "GET or POST", 405)
	}
}

func (d *daemon) handlePublish(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", 405)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		http.Error(w, err.Error(), 400)
		return
	}
	v, err := canon.DecodeJSON(raw)
	if err != nil {
		http.Error(w, "invalid json", 400)
		return
	}
	m, ok := v.(map[string]interface{})
	if !ok {
		http.Error(w, "object required", 400)
		return
	}
	if topicFor(fmt.Sprint(m["kind"])) == "" {
		http.Error(w, "unknown kind", 400)
		return
	}
	if err := requireContentCID(fmt.Sprint(m["kind"]), stringOf(m, "cid")); err != nil {
		http.Error(w, err.Error(), 400)
		return
	}
	if err := requireReportReason(fmt.Sprint(m["kind"]), stringOf(m, "reason")); err != nil {
		http.Error(w, err.Error(), 400)
		return
	}
	m["publisher"] = d.host.ID().String()
	m["v"] = json.Number("1")
	m["club"] = d.clubID
	canonBytes, err := canon.Marshal(m)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	sig, err := d.priv.Sign(canonBytes)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	if err := d.applyClaimPolicy(m); err != nil {
		http.Error(w, err.Error(), 429)
		return
	}
	if d.limiter != nil {
		sum := sha256.Sum256(canonBytes)
		_ = d.limiter.Duplicate(hex.EncodeToString(sum[:]))
	}
	m["sig"] = hex.EncodeToString(sig)
	out, err := json.Marshal(m)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	kind := topicFor(fmt.Sprint(m["kind"]))
	if err := d.appendInbox(out); err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	if err := d.broadcast(r.Context(), kind, out); err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(out)
}

func (d *daemon) broadcast(ctx context.Context, kind string, payload []byte) error {
	t := d.joined[kind]
	if t == nil {
		return fmt.Errorf("unknown kind topic %s", kind)
	}
	return t.Publish(ctx, payload)
}

func (d *daemon) consume(ctx context.Context, sub *pubsub.Subscription) {
	for {
		msg, err := sub.Next(ctx)
		if err != nil {
			return
		}
		if msg.ReceivedFrom == d.host.ID() {
			continue
		}
		if len(msg.Data) > 1<<20 {
			log.Printf("drop gossip: too large")
			continue
		}
		if err := d.acceptGossip(msg.Data, true); err != nil {
			log.Printf("drop gossip: %v", err)
		}
	}
}

func (d *daemon) acceptGossip(data []byte, rateLimit bool) error {
	v, err := canon.DecodeJSON(data)
	if err != nil {
		return err
	}
	m, ok := v.(map[string]interface{})
	if !ok {
		return fmt.Errorf("not an object")
	}
	sigHex, _ := m["sig"].(string)
	if sigHex == "" {
		return fmt.Errorf("missing sig")
	}
	sig, err := hex.DecodeString(sigHex)
	if err != nil {
		return err
	}
	pubStr, _ := m["publisher"].(string)
	pid, err := peer.Decode(pubStr)
	if err != nil {
		return fmt.Errorf("publisher: %w", err)
	}
	pk := d.host.Peerstore().PubKey(pid)
	if pk == nil {
		// Reconstruct from peer ID (CIDv1 / identity hash of the key).
		pk, err = pid.ExtractPublicKey()
		if err != nil {
			return fmt.Errorf("no pubkey for %s: %w", pid, err)
		}
	}
	canonBytes, err := canon.Marshal(m)
	if err != nil {
		return err
	}
	okSig, err := pk.Verify(canonBytes, sig)
	if err != nil || !okSig {
		return fmt.Errorf("bad signature")
	}
	if c := stringOf(m, "club"); c != "" && c != d.clubID {
		return fmt.Errorf("wrong club %s", c)
	}
	if err := requireContentCID(fmt.Sprint(m["kind"]), stringOf(m, "cid")); err != nil {
		return err
	}
	if err := requireReportReason(fmt.Sprint(m["kind"]), stringOf(m, "reason")); err != nil {
		return err
	}
	sum := sha256.Sum256(canonBytes)
	payloadHash := hex.EncodeToString(sum[:])
	if d.limiter != nil && d.limiter.Duplicate(payloadHash) {
		return nil
	}
	if rateLimit && d.limiter != nil && !d.limiter.Allow(pubStr) {
		return fmt.Errorf("rate limited %s", pubStr)
	}
	if err := d.applyClaimPolicy(m); err != nil {
		return err
	}
	return d.appendInbox(data)
}

func (d *daemon) applyClaimPolicy(m map[string]interface{}) error {
	kind := fmt.Sprint(m["kind"])
	pub := stringOf(m, "publisher")
	cid := stringOf(m, "cid")
	switch kind {
	case "claim":
		if !d.claims.Allow(pub, cid, untilOf(m)) {
			return fmt.Errorf("one in-flight claim per peer")
		}
	case "classify", "skip":
		d.claims.Clear(pub, cid)
	}
	return nil
}

type mdnsNotifee struct {
	h host.Host
}

func (n *mdnsNotifee) HandlePeerFound(pi peer.AddrInfo) {
	if pi.ID == n.h.ID() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := n.h.Connect(ctx, pi); err != nil {
		log.Printf("mdns connect %s: %v", pi.ID, err)
		return
	}
	log.Printf("mdns connected %s", pi.ID)
}

func (d *daemon) appendInbox(line []byte) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	name := time.Now().UTC().Format("2006-01-02") + ".jsonl"
	path := filepath.Join(d.inbox, name)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		return err
	}
	_ = pruneInboxDir(d.inbox, d.inboxKeepDays, d.inboxMaxBytes)
	return nil
}

func topicFor(kind string) string {
	switch kind {
	case "claim", "skip", "classify", "alias", "report":
		return kind
	default:
		return ""
	}
}

func loadOrCreateKey(path string) (crypto.PrivKey, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	if b, err := os.ReadFile(path); err == nil && len(b) > 0 {
		return crypto.UnmarshalPrivateKey(b)
	}
	priv, _, err := crypto.GenerateEd25519Key(rand.Reader)
	if err != nil {
		return nil, err
	}
	b, err := crypto.MarshalPrivateKey(priv)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, b, 0o600); err != nil {
		return nil, err
	}
	log.Printf("wrote new identity to %s", path)
	return priv, nil
}

func (d *daemon) bootstrapPeers() []string {
	d.syncMu.Lock()
	defer d.syncMu.Unlock()
	out := make([]string, len(d.bootstrap))
	copy(out, d.bootstrap)
	return out
}

func (d *daemon) setBootstrapPeers(peers []string) {
	d.syncMu.Lock()
	defer d.syncMu.Unlock()
	d.bootstrap = append([]string(nil), peers...)
}

func splitBootstrap(list string) []string {
	parts := strings.Split(list, ",")
	out := make([]string, 0, len(parts))
	seen := map[string]bool{}
	for _, raw := range parts {
		raw = strings.TrimSpace(raw)
		if raw == "" || seen[raw] {
			continue
		}
		seen[raw] = true
		out = append(out, raw)
	}
	return out
}

func parseBootstrapAddr(raw string) (peer.AddrInfo, error) {
	ma, err := multiaddr.NewMultiaddr(raw)
	if err != nil {
		return peer.AddrInfo{}, err
	}
	ai, err := peer.AddrInfoFromP2pAddr(ma)
	if err != nil {
		return peer.AddrInfo{}, err
	}
	return *ai, nil
}

func normalizeBootstrapPeers(in []string) ([]string, error) {
	out := make([]string, 0, len(in))
	seen := map[string]bool{}
	for _, raw := range in {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			continue
		}
		if _, err := parseBootstrapAddr(raw); err != nil {
			return nil, fmt.Errorf("%s: %w", raw, err)
		}
		if seen[raw] {
			continue
		}
		seen[raw] = true
		out = append(out, raw)
	}
	if len(out) > 32 {
		return nil, fmt.Errorf("at most 32 bootstrap peers")
	}
	return out, nil
}

func connectBootstrap(ctx context.Context, h host.Host, peers []string) []bootstrapDial {
	out := make([]bootstrapDial, 0, len(peers))
	for _, raw := range peers {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			continue
		}
		res := bootstrapDial{Addr: raw}
		ai, err := parseBootstrapAddr(raw)
		if err != nil {
			res.Error = err.Error()
			log.Printf("bootstrap addr %s: %v", raw, err)
			out = append(out, res)
			continue
		}
		res.Peer = ai.ID.String()
		if err := h.Connect(ctx, ai); err != nil {
			res.Error = err.Error()
			log.Printf("bootstrap connect %s: %v", ai.ID, err)
			out = append(out, res)
			continue
		}
		log.Printf("connected to %s", ai.ID)
		out = append(out, res)
	}
	return out
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}
