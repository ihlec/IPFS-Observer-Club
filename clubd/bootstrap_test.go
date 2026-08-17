package main

import "testing"

const testPeer = "12D3KooWMRcoucT8Mp2nSYC89y9hKkBWpVXRkUu6oyDhdUowEZnQ"

func TestParseBootstrapAddr(t *testing.T) {
	ok := "/ip4/203.0.113.8/tcp/4713/p2p/" + testPeer
	ai, err := parseBootstrapAddr(ok)
	if err != nil {
		t.Fatalf("valid addr: %v", err)
	}
	if ai.ID.String() != testPeer {
		t.Fatalf("peer %s", ai.ID)
	}
	if _, err := parseBootstrapAddr("/ip4/203.0.113.8/tcp/4713"); err == nil {
		t.Fatal("missing /p2p/ should fail")
	}
	if _, err := parseBootstrapAddr("not-a-multiaddr"); err == nil {
		t.Fatal("garbage should fail")
	}
}

func TestNormalizeBootstrapPeers(t *testing.T) {
	a := "/ip4/203.0.113.8/tcp/4713/p2p/" + testPeer
	b := "/dns4/observer.example/tcp/4713/p2p/" + testPeer
	got, err := normalizeBootstrapPeers([]string{"", a, a, "  " + b + "  "})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != a || got[1] != b {
		t.Fatalf("got %v", got)
	}
	if _, err := normalizeBootstrapPeers([]string{"/ip4/1.2.3.4/tcp/4713"}); err == nil {
		t.Fatal("invalid addr should fail")
	}
	if got, err := normalizeBootstrapPeers(nil); err != nil || len(got) != 0 {
		t.Fatalf("empty %v %v", got, err)
	}
}

func TestSplitBootstrap(t *testing.T) {
	a := "/ip4/1.2.3.4/tcp/4713/p2p/" + testPeer
	got := splitBootstrap(a + "," + a + ", ")
	if len(got) != 1 || got[0] != a {
		t.Fatalf("got %v", got)
	}
}
