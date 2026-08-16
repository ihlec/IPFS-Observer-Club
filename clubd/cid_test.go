package main

import "testing"

func TestRequireContentCID(t *testing.T) {
	if err := requireContentCID("alias", ""); err != nil {
		t.Fatalf("alias: %v", err)
	}
	if err := requireContentCID("classify", "bafya"); err == nil {
		t.Fatal("placeholder cid should be rejected")
	}
	if err := requireContentCID("skip", "bafyb"); err == nil {
		t.Fatal("placeholder cid should be rejected")
	}
	ok := "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
	if err := requireContentCID("classify", ok); err != nil {
		t.Fatalf("real cid: %v", err)
	}
}

func TestRequireReportReason(t *testing.T) {
	if err := requireReportReason("classify", ""); err != nil {
		t.Fatalf("classify: %v", err)
	}
	if err := requireReportReason("report", "wrong"); err != nil {
		t.Fatalf("wrong: %v", err)
	}
	if err := requireReportReason("report", "abusive"); err != nil {
		t.Fatalf("abusive: %v", err)
	}
	if err := requireReportReason("report", "clear"); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if err := requireReportReason("report", "spam"); err == nil {
		t.Fatal("spam should be rejected")
	}
}
