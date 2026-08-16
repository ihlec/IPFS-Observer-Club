// Passive Bitswap sniffer: connects to IPFS peers, listens for incoming
// wantlist broadcasts, and appends observed (cid, peer, ts) records to
// rotating JSONL spool files. It never requests or serves content.
package main

import (
	"context"
	"crypto/rand"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	bsmsg "github.com/ipfs/boxo/bitswap/message"
	lru "github.com/hashicorp/golang-lru/v2"
	"github.com/libp2p/go-libp2p"
	dht "github.com/libp2p/go-libp2p-kad-dht"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/protocol"
	"github.com/libp2p/go-libp2p/p2p/net/connmgr"
	"github.com/libp2p/go-msgio"
)

var bitswapProtocols = []protocol.ID{
	"/ipfs/bitswap/1.2.0",
	"/ipfs/bitswap/1.1.0",
	"/ipfs/bitswap/1.0.0",
	"/ipfs/bitswap",
}

type record struct {
	TS   int64  `json:"ts"`
	CID  string `json:"cid"`
	Peer string `json:"peer"`
}

func main() {
	var (
		port     = flag.Int("port", 4712, "libp2p listen port")
		lowConns = flag.Int("low", 50, "connection manager low watermark")
		hiConns  = flag.Int("high", 80, "connection manager high watermark")
		spoolDir = flag.String("spool", "data/spool", "spool output directory")
		interval = flag.Duration("interval", 30*time.Second, "peer discovery interval")
	)
	flag.Parse()

	if err := os.MkdirAll(*spoolDir, 0o755); err != nil {
		log.Fatalf("creating spool dir: %v", err)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	cm, err := connmgr.NewConnManager(*lowConns, *hiConns, connmgr.WithGracePeriod(time.Minute))
	if err != nil {
		log.Fatalf("connmgr: %v", err)
	}

	host, err := libp2p.New(
		libp2p.ListenAddrStrings(
			fmt.Sprintf("/ip4/0.0.0.0/tcp/%d", *port),
			fmt.Sprintf("/ip4/0.0.0.0/udp/%d/quic-v1", *port),
		),
		libp2p.ConnectionManager(cm),
		libp2p.UserAgent("ipfs-observer-club-sniffer/0.1"),
	)
	if err != nil {
		log.Fatalf("libp2p host: %v", err)
	}
	defer host.Close()
	log.Printf("sniffer peer ID: %s", host.ID())

	// Dedup (cid, peer) pairs so repeated rebroadcasts don't bloat the spool.
	seen, err := lru.New[string, struct{}](1 << 17)
	if err != nil {
		log.Fatalf("lru: %v", err)
	}

	records := make(chan record, 4096)
	go spoolWriter(ctx, *spoolDir, records)

	handler := func(s network.Stream) {
		defer s.Close()
		peerID := s.Conn().RemotePeer().String()
		reader := msgio.NewVarintReaderSize(s, network.MessageSizeMax)
		for {
			msg, _, err := bsmsg.FromMsgReader(reader)
			if err != nil {
				return
			}
			now := time.Now().Unix()
			for _, e := range msg.Wantlist() {
				if e.Cancel {
					continue
				}
				codec := e.Cid.Prefix().Codec
				// Folders and identity blocks are not documents. Skipping
				// them here keeps the spool small for every club peer.
				if codec == 0x70 || codec == 0x71 || codec == 0x72 ||
					codec == 0x0129 || codec == 0x0200 { // dag-pb, dag-cbor, libp2p-key, dag-json, json
					continue
				}
				c := e.Cid.String()
				key := c + "|" + peerID
				if _, dup := seen.Get(key); dup {
					continue
				}
				seen.Add(key, struct{}{})
				select {
				case records <- record{TS: now, CID: c, Peer: peerID}:
				default: // drop rather than block the stream
				}
			}
		}
	}
	for _, p := range bitswapProtocols {
		host.SetStreamHandler(p, handler)
	}

	// DHT in client mode: used purely to discover and connect to peers.
	kdht, err := dht.New(ctx, host,
		dht.Mode(dht.ModeClient),
		dht.BootstrapPeers(dht.GetDefaultBootstrapPeerAddrInfos()...),
	)
	if err != nil {
		log.Fatalf("dht: %v", err)
	}
	if err := kdht.Bootstrap(ctx); err != nil {
		log.Fatalf("dht bootstrap: %v", err)
	}

	go discoveryLoop(ctx, kdht, *interval)
	go statsLoop(ctx, host, records)

	<-ctx.Done()
	log.Println("shutting down")
}

// discoveryLoop performs random-walk lookups so the routing table keeps
// yielding fresh peers for the connection manager to hold on to.
func discoveryLoop(ctx context.Context, kdht *dht.IpfsDHT, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			key := make([]byte, 32)
			if _, err := rand.Read(key); err != nil {
				continue
			}
			lookupCtx, cancel := context.WithTimeout(ctx, interval)
			_, _ = kdht.GetClosestPeers(lookupCtx, string(key))
			cancel()
		}
	}
}

func statsLoop(ctx context.Context, h interface{ Network() network.Network }, records chan record) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			log.Printf("connections=%d spool-queue=%d", len(h.Network().Conns()), len(records))
		}
	}
}

// spoolWriter appends JSONL records to 5-minute files. ingest.py only
// picks up a file once a newer bucket exists.
func spoolWriter(ctx context.Context, dir string, records <-chan record) {
	var (
		f       *os.File
		curName string
	)
	defer func() {
		if f != nil {
			f.Close()
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return
		case r := <-records:
			// Rotate every 5 minutes so the ingester picks up fresh CIDs
			// promptly (a file is consumed once the next bucket starts).
			bucket := time.Now().UTC().Truncate(5 * time.Minute)
			name := fmt.Sprintf("%s/cids-%s.jsonl", dir, bucket.Format("20060102-150405"))
			if name != curName {
				if f != nil {
					f.Close()
				}
				var err error
				f, err = os.OpenFile(name, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
				if err != nil {
					log.Printf("spool open: %v", err)
					continue
				}
				curName = name
			}
			fmt.Fprintf(f, "{\"ts\":%d,\"cid\":%q,\"peer\":%q}\n", r.TS, r.CID, r.Peer)
		}
	}
}
