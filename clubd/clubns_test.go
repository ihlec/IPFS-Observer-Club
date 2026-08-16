package main

import "testing"

func TestValidateClubID(t *testing.T) {
	got, err := normalizeClubID("Academic")
	if err != nil || got != "academic" {
		t.Fatalf("normalize Academic: %q %v", got, err)
	}
	if err := validateClubID("academic"); err != nil {
		t.Fatal(err)
	}
	if err := validateClubID("field-recordings"); err != nil {
		t.Fatal(err)
	}
	if err := validateClubID("a"); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"", "-x", "x-", "has_underscore", "spaces no"} {
		if err := validateClubID(bad); err == nil {
			t.Fatalf("expected error for %q", bad)
		}
	}
}

func TestNamespace(t *testing.T) {
	if got := topicPrefixFor("academic"); got != "ipfs-observer-club/v1/academic/" {
		t.Fatalf("topic prefix %s", got)
	}
	if got := mdnsServiceFor("academic"); got != "ipfs-observer-club-academic" {
		t.Fatalf("mdns %s", got)
	}
	if got := string(snapshotProtoFor("academic")); got != "/ipfs-observer-club/v1/academic/snapshot" {
		t.Fatalf("snapshot proto %s", got)
	}
}
