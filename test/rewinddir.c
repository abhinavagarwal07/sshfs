/* Run against a directory containing file-000000-<160 zeros> through
 * file-002047-<160 zeros>, as created by test_rewinddir.py. */
#define _DEFAULT_SOURCE
#define _DARWIN_C_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define NFILES 2048

static void fail(const char *message)
{
	fprintf(stderr, "%s\n", message);
	exit(1);
}

/* Return a validated file index, skipping dot entries; -1 denotes EOF. */
static int read_entry(DIR *dir)
{
	struct dirent *entry;

	for (;;) {
		unsigned index;
		char expected[256];
		errno = 0;
		entry = readdir(dir);
		if (!entry) {
			if (errno)
				fail("readdir failed");
			return -1;
		}
		if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, ".."))
			continue;
		if (sscanf(entry->d_name, "file-%u-", &index) != 1 || index >= NFILES)
			fail("unexpected directory entry");
		snprintf(expected, sizeof(expected), "file-%06u-%0160u", index, 0U);
		if (strcmp(entry->d_name, expected))
			fail("unexpected directory entry");
		return (int)index;
	}
}

static void check_listing(DIR *dir)
{
	int seen[NFILES] = { 0 };
	unsigned count = 0;
	int index;

	while ((index = read_entry(dir)) != -1) {
		if (seen[index]++)
			fail("duplicate directory entry");
		count++;
	}
	if (count != NFILES)
		fail("incomplete directory listing");
}

static void check_seek(DIR *dir)
{
	int entries[NFILES], seen[NFILES] = { 0 };
	long position = -1;

	/* Save a cookie after several buffers, then read all the way to EOF. */
	for (unsigned i = 0; i < NFILES; i++) {
		entries[i] = read_entry(dir);
		if (entries[i] == -1 || seen[entries[i]]++)
			fail("incomplete or duplicate directory listing before seekdir");
		if (i + 1 == NFILES / 2) {
			position = telldir(dir);
			if (position == -1)
				fail("telldir failed");
		}
	}
	if (read_entry(dir) != -1)
		fail("extra directory entries before seekdir");
	seekdir(dir, position);
	for (unsigned i = NFILES / 2; i < NFILES; i++) {
		if (read_entry(dir) != entries[i])
			fail("seekdir did not replay the same stream suffix");
	}
	if (read_entry(dir) != -1)
		fail("extra directory entries after seekdir");
}

static void check_rename(DIR *dir, const char *path)
{
	char renamed[PATH_MAX], marker[PATH_MAX];
	int fd;

	if (snprintf(renamed, sizeof(renamed), "%s.renamed", path) >= (int)sizeof(renamed) ||
	    snprintf(marker, sizeof(marker), "%s/replacement-marker", path) >= (int)sizeof(marker))
		fail("test path is too long");

	/* Rewinding must follow the open directory after it is renamed. */
	if (rename(path, renamed))
		fail("rename failed");
	rewinddir(dir);
	check_listing(dir);

	/* Replacing the old path must not redirect that same directory stream. */
	if (mkdir(path, 0700))
		fail("mkdir replacement failed");
	fd = open(marker, O_WRONLY | O_CREAT | O_EXCL, 0600);
	if (fd == -1 || close(fd))
		fail("create replacement marker failed");
	rewinddir(dir);
	check_listing(dir);
	if (unlink(marker) || rmdir(path) || rename(renamed, path))
		fail("restore test directory failed");
}

int main(int argc, char **argv)
{
	DIR *dir;
	const char *operation = argc == 3 ? argv[2] : "rename";

	if (argc < 2 || argc > 3)
		fail("usage: rewinddir MOUNTED_DIRECTORY [rename|rewind-eof|rewind-partial|seek]");
	dir = opendir(argv[1]);
	if (!dir)
		fail("opendir failed");
	check_listing(dir);

	if (!strcmp(operation, "rename")) {
		check_rename(dir, argv[1]);
	} else if (!strcmp(operation, "rewind-eof")) {
		for (unsigned i = 0; i < 3; i++) {
			rewinddir(dir);
			check_listing(dir);
		}
	} else if (!strcmp(operation, "rewind-partial")) {
		rewinddir(dir);
		for (unsigned i = 0; i < NFILES / 2; i++) {
			if (read_entry(dir) == -1)
				fail("partial read ended early");
		}
		rewinddir(dir);
		check_listing(dir);
	} else if (!strcmp(operation, "seek")) {
		rewinddir(dir);
		check_seek(dir);
	} else {
		fail("unknown directory operation");
	}
	if (closedir(dir))
		fail("closedir failed");
	return 0;
}
