CC ?= cc
CPPFLAGS += -I../include
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic
.PHONY: all clean
all: ace-run
ace-run: ace-run.c run_config.c run_config.h ace_manifest.c ace_manifest.h ../include/ace_run_control.h
	$(CC) $(CPPFLAGS) $(CFLAGS) ace-run.c run_config.c ace_manifest.c -o $@
clean:
	rm -f ace-run
