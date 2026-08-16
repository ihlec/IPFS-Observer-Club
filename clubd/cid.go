package main

import (
	"fmt"

	cid "github.com/ipfs/go-cid"
)

func requireContentCID(kind, c string) error {
	if kind == "alias" {
		return nil
	}
	if _, err := cid.Decode(c); err != nil {
		return fmt.Errorf("invalid cid")
	}
	return nil
}

func requireReportReason(kind, reason string) error {
	if kind != "report" {
		return nil
	}
	if reason == "wrong" || reason == "abusive" || reason == "clear" {
		return nil
	}
	return fmt.Errorf("bad report reason")
}
