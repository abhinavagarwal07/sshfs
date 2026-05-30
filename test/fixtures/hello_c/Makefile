CC ?= gcc
CFLAGS ?= -Wall -Wextra

hello: main.o util.o
	$(CC) $(CFLAGS) -o $@ $^

%.o: %.c util.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o hello

.PHONY: clean
