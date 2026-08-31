CC ?= cc
CPPFLAGS += -Iinclude
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic

BIN_DIR := bin
TARGETS := $(BIN_DIR)/sender $(BIN_DIR)/receiver

.PHONY: all clean

all: $(TARGETS)

$(BIN_DIR):
	mkdir -p $@

$(BIN_DIR)/sender: src/sender.c include/config.h include/protocol.h | $(BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@

$(BIN_DIR)/receiver: src/receiver.c include/config.h include/protocol.h | $(BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@

clean:
	rm -f $(TARGETS)
	rmdir $(BIN_DIR) 2>/dev/null || true
