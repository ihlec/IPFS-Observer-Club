PY := ./venv/bin/python
PIP := ./venv/bin/pip
CLUBD := ./build/clubd
SNIFFER := ./build/sniffer
LOG := data/observer.log
CLUBD_ARGS := $(shell $(PY) -m observer.clubd_flags 2>/dev/null)

.PHONY: help setup build test start stop web clubd sniff clean

help:
	@echo "IPFS Observer Club"
	@echo "  make setup   venv + deps + clubd + sniffer"
	@echo "  make test    Go + Python tests"
	@echo "  make start   clubd + observer (sniffer + indexer + web)"
	@echo "  make stop    stop background processes"
	@echo "  make web     Python UI only (needs clubd for publish)"
	@echo "  make clubd   run gossip daemon in the foreground"
	@echo "  make sniff   run the Bitswap sniffer in the foreground"

setup: build
	python3 -m venv venv
	$(PIP) install -q -r requirements.txt
	@test -f config.toml || cp config.toml.example config.toml
	@echo "setup complete — edit config.toml, then make start"

build:
	mkdir -p build
	cd clubd && go build -o ../build/clubd .
	cd sniffer && go build -o ../build/sniffer .

test:
	cd clubd && go test ./...
	cd sniffer && go test ./...
	$(PY) -m pytest -q

start: build
	@mkdir -p data
	@test -f config.toml || cp config.toml.example config.toml
	@$(PY) -c "from observer import config; config.migrate_legacy_paths()"
	@nohup $(CLUBD) $(CLUBD_ARGS) >> data/clubd.log 2>&1 & echo $$! > data/clubd.pid
	@API=$$($(PY) -c "from observer import config; print('%s:%s' % (config.API_HOST, config.API_PORT))"); \
	  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25; do \
	    curl -sf "http://$$API/health" >/dev/null && break; \
	    sleep 0.2; \
	  done
	@nohup $(PY) -m observer.main >> $(LOG) 2>&1 & echo $$! > data/observer.pid
	@echo "clubd + observer started"
	@echo "web UI:  http://127.0.0.1:8002"
	@echo "clubd:   http://127.0.0.1:8003/id"
	@echo "logs:    tail -f data/observer.log data/clubd.log data/sniffer.log"

stop:
	@if [ -f data/observer.pid ]; then kill $$(cat data/observer.pid) 2>/dev/null || true; rm -f data/observer.pid; fi
	@if [ -f data/clubd.pid ]; then kill $$(cat data/clubd.pid) 2>/dev/null || true; rm -f data/clubd.pid; fi
	@pkill -f ' -m observer.main' >/dev/null 2>&1 || true
	@pkill -f 'build/sniffer -port' >/dev/null 2>&1 || true
	@for p in 8002 8003 4712 4713; do \
	  pids=$$(lsof -tiTCP:$$p -sTCP:LISTEN 2>/dev/null || true); \
	  if [ -n "$$pids" ]; then kill $$pids 2>/dev/null || true; fi; \
	done
	@sleep 0.4
	@echo "stopped"

web:
	$(PY) -m observer.web

clubd: build
	@$(PY) -c "from observer import config; config.migrate_legacy_paths()"
	$(CLUBD) $(CLUBD_ARGS)

sniff: build
	$(SNIFFER) -port 4712 -low 50 -high 80 -spool data/spool

clean:
	rm -rf build data/club.sqlite data/club.sqlite-* data/*/club.sqlite data/*/club.sqlite-* data/work.sqlite data/work.sqlite-* data/inbox/*.jsonl data/*/inbox/*.jsonl data/spool/*.jsonl
