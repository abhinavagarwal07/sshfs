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
#define FRESH_NAME "fresh-after-rename"

static void fail(const char *message)
{
	fprintf(stderr, "%s\n", message);
	exit(1);
}

static void create_file(const char *path)
{
	int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
	if (fd == -1 || close(fd))
		fail("create file failed");
}

static void check_listing(DIR *dir, int expect_fresh)
{
	int seen[NFILES] = { 0 };
	int saw_fresh = 0;
	unsigned count = 0;
	struct dirent *entry;

	for (;;) {
		unsigned index;
		char expected[256];
		errno = 0;
		entry = readdir(dir);
		if (!entry) {
			if (errno)
				fail("readdir failed");
			break;
		}
		if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, ".."))
			continue;
		if (!strcmp(entry->d_name, FRESH_NAME)) {
			if (saw_fresh++)
				fail("duplicate fresh directory entry");
			count++;
			continue;
		}
		if (sscanf(entry->d_name, "file-%u-", &index) != 1 || index >= NFILES)
			fail("unexpected directory entry");
		snprintf(expected, sizeof(expected), "file-%06u-%0160u", index, 0U);
		if (strcmp(entry->d_name, expected) || seen[index]++)
			fail("unexpected or duplicate directory entry");
		count++;
	}
	if (count != NFILES + (unsigned) expect_fresh ||
	    saw_fresh != expect_fresh)
		fail("incomplete directory listing");
}

int main(int argc, char **argv)
{
	char renamed[PATH_MAX], marker[PATH_MAX], fresh[PATH_MAX];
	DIR *dir, *unread;

	if (argc != 2)
		fail("usage: rewinddir MOUNTED_DIRECTORY");
	if (snprintf(renamed, sizeof(renamed), "%s.renamed", argv[1]) >= (int)sizeof(renamed) ||
	    snprintf(marker, sizeof(marker), "%s/replacement-marker", argv[1]) >= (int)sizeof(marker) ||
	    snprintf(fresh, sizeof(fresh), "%s/%s", renamed, FRESH_NAME) >= (int)sizeof(fresh))
		fail("test path is too long");
	dir = opendir(argv[1]);
	unread = opendir(argv[1]);
	if (!dir || !unread)
		fail("opendir failed");
	check_listing(dir, 0);

	/* After a rename, with another directory at the old path, both the
	 * rewound and the unread stream must list the renamed directory and its
	 * new entry. Read dir first: listing unread would refill the directory
	 * cache, and the rewind would then never reach sshfs. */
	if (rename(argv[1], renamed) || mkdir(argv[1], 0700))
		fail("rename or mkdir replacement failed");
	create_file(marker);
	create_file(fresh);
	rewinddir(dir);
	check_listing(dir, 1);
	check_listing(unread, 1);
	if (closedir(unread) || closedir(dir))
		fail("closedir failed");

	if (unlink(marker) || rmdir(argv[1]) || unlink(fresh) ||
	    rename(renamed, argv[1]))
		fail("restore test directory failed");
	return 0;
}
