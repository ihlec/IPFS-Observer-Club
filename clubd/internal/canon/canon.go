package canon

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
)

// Marshal produces the same bytes as Python
// json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
// with the "sig" key omitted. Numbers must be json.Number or integers;
// float64 values that are whole numbers are emitted without a decimal.
func Marshal(v interface{}) ([]byte, error) {
	buf := &bytes.Buffer{}
	if err := write(buf, stripSig(v)); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func stripSig(v interface{}) interface{} {
	switch t := v.(type) {
	case map[string]interface{}:
		out := make(map[string]interface{}, len(t))
		for k, val := range t {
			if k == "sig" {
				continue
			}
			out[k] = stripSig(val)
		}
		return out
	case []interface{}:
		out := make([]interface{}, len(t))
		for i, val := range t {
			out[i] = stripSig(val)
		}
		return out
	default:
		return v
	}
}

func write(buf *bytes.Buffer, v interface{}) error {
	switch t := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if t {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case json.Number:
		buf.WriteString(string(t))
	case int:
		buf.WriteString(strconv.Itoa(t))
	case int64:
		buf.WriteString(strconv.FormatInt(t, 10))
	case float64:
		if t == float64(int64(t)) {
			buf.WriteString(strconv.FormatInt(int64(t), 10))
		} else {
			buf.WriteString(strconv.FormatFloat(t, 'f', -1, 64))
		}
	case string:
		b, err := json.Marshal(t)
		if err != nil {
			return err
		}
		// json.Marshal on string uses HTML escaping; undo that.
		b = bytes.ReplaceAll(b, []byte(`\u0026`), []byte(`&`))
		b = bytes.ReplaceAll(b, []byte(`\u003c`), []byte(`<`))
		b = bytes.ReplaceAll(b, []byte(`\u003e`), []byte(`>`))
		buf.Write(b)
	case map[string]interface{}:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			kb, err := json.Marshal(k)
			if err != nil {
				return err
			}
			buf.Write(kb)
			buf.WriteByte(':')
			if err := write(buf, t[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	case []interface{}:
		buf.WriteByte('[')
		for i, el := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := write(buf, el); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	default:
		return fmt.Errorf("canon: unsupported type %T", v)
	}
	return nil
}

// DecodeJSON unmarshals using json.Number so integers stay integers.
func DecodeJSON(data []byte) (interface{}, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var v interface{}
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return v, nil
}
