CC ?= cc
CPPFLAGS += -Iinclude -I../include
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic

BIN_DIR := bin
TARGETS := $(BIN_DIR)/sender $(BIN_DIR)/receiver $(BIN_DIR)/phc_calibrate $(BIN_DIR)/ace_clock

.PHONY: all clean

all: $(TARGETS)

$(BIN_DIR):
	mkdir -p $@

$(BIN_DIR)/sender: src/sender.c include/config.h include/protocol.h ../include/ace_packet_abi.h ../include/ace_run_control.h | $(BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@

$(BIN_DIR)/receiver: src/receiver.c include/config.h include/protocol.h \
		../include/ace_packet_abi.h ../include/ace_hwtstamp.h ../include/ace_run_control.h | $(BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@

$(BIN_DIR)/phc_calibrate: src/phc_calibrate.c | $(BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@

$(BIN_DIR)/ace_clock: src/ace_clock.c ../include/ace_run_control.h | $(BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@

clean:
	rm -f $(TARGETS)
	rmdir $(BIN_DIR) 2>/dev/null || true
