CC = gcc
CFLAGS = -Wall -O2

hello: main.o util.o
	$(CC) $(CFLAGS) -o hello main.o util.o

main.o: main.c util.h
	$(CC) $(CFLAGS) -c main.c

util.o: util.c util.h
	$(CC) $(CFLAGS) -c util.c

clean:
	rm -f hello *.o
