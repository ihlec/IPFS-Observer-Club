package canon

import (
	"encoding/json"
	"testing"
)

func TestMatchesPythonIntsAndOrder(t *testing.T) {
	raw := []byte(`{"kind":"claim","until":900,"cid":"bafy","v":1,"sig":"dead"}`)
	v, err := DecodeJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	got, err := Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	// sig stripped, keys sorted, until stays 900 not 900.0
	want := `{"cid":"bafy","kind":"claim","until":900,"v":1}`
	if string(got) != want {
		t.Fatalf("got %s want %s", got, want)
	}
}

func TestNestedAndUnicode(t *testing.T) {
	raw := []byte(`{"z":{"b":2,"a":1},"m":"café","sig":"x"}`)
	v, err := DecodeJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	got, err := Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"m":"café","z":{"a":1,"b":2}}`
	if string(got) != want {
		t.Fatalf("got %s want %s", got, want)
	}
}

func TestRoundTripNumber(t *testing.T) {
	var n json.Number = "900"
	got, err := Marshal(map[string]interface{}{"until": n})
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != `{"until":900}` {
		t.Fatalf("got %s", got)
	}
}
