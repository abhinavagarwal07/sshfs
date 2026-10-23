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

static void check_empty(DIR *dir, int allow_estale)
{
	struct dirent *entry;

	errno = 0;
	while ((entry = readdir(dir)) != NULL) {
		if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, ".."))
			fail("removed stream followed replacement directory");
	}
	if (errno && !(allow_estale && errno == ESTALE))
		fail("removed directory readdir failed");
}

int main(int argc, char **argv)
{
	char renamed[PATH_MAX], marker[PATH_MAX], fresh[PATH_MAX];
	char removed[PATH_MAX], removed_marker[PATH_MAX];
	DIR *dir, *unread, *removed_open, *removed_cached;

	if (argc != 2)
		fail("usage: rewinddir MOUNTED_DIRECTORY");
	if (snprintf(renamed, sizeof(renamed), "%s.renamed", argv[1]) >= (int)sizeof(renamed) ||
	    snprintf(marker, sizeof(marker), "%s/replacement-marker", argv[1]) >= (int)sizeof(marker) ||
	    snprintf(fresh, sizeof(fresh), "%s/%s", renamed, FRESH_NAME) >= (int)sizeof(fresh) ||
	    snprintf(removed, sizeof(removed), "%s.removed", argv[1]) >= (int)sizeof(removed) ||
	    snprintf(removed_marker, sizeof(removed_marker),
		     "%s/replacement-marker", removed) >= (int)sizeof(removed_marker))
		fail("test path is too long");
	dir = opendir(argv[1]);
	unread = opendir(argv[1]);
	if (!dir || !unread)
		fail("opendir failed");
	check_listing(dir, 0);

	/* Both unread and exhausted streams must follow a rename and refresh. */
	if (rename(argv[1], renamed))
		fail("rename failed");
	create_file(fresh);
	check_listing(unread, 1);
	rewinddir(dir);
	check_listing(dir, 1);

	/* Replacing the old path must not redirect that same directory stream. */
	if (mkdir(argv[1], 0700))
		fail("mkdir replacement failed");
	create_file(marker);
	rewinddir(dir);
	check_listing(dir, 1);
	rewinddir(unread);
	check_listing(unread, 1);
	if (closedir(unread) || closedir(dir))
		fail("closedir failed");

	if (unlink(marker) || rmdir(argv[1]) || unlink(fresh) ||
	    rename(renamed, argv[1]))
		fail("restore test directory failed");

	/* A removed open stream must not follow a replacement at its old path.
	 * The second stream is served only from the directory cache when enabled. */
	if (mkdir(removed, 0700))
		fail("mkdir removed test directory failed");
	removed_open = opendir(removed);
	if (!removed_open)
		fail("opendir removed test directory failed");
	check_empty(removed_open, 0);
	removed_cached = opendir(removed);
	if (!removed_cached)
		fail("opendir cached removed test directory failed");
	check_empty(removed_cached, 0);
	if (rmdir(removed) || mkdir(removed, 0700))
		fail("replace removed test directory failed");
	create_file(removed_marker);
	rewinddir(removed_open);
	check_empty(removed_open, 0);
	rewinddir(removed_cached);
	check_empty(removed_cached, 1);
	if (closedir(removed_cached) || closedir(removed_open) ||
	    unlink(removed_marker) || rmdir(removed))
		fail("cleanup removed test directory failed");
	return 0;
}
