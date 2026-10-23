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

static void check_listing(DIR *dir)
{
	int seen[NFILES] = { 0 };
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
		if (sscanf(entry->d_name, "file-%u-", &index) != 1 || index >= NFILES)
			fail("unexpected directory entry");
		snprintf(expected, sizeof(expected), "file-%06u-%0160u", index, 0U);
		if (strcmp(entry->d_name, expected) || seen[index]++)
			fail("unexpected or duplicate directory entry");
		count++;
	}
	if (count != NFILES)
		fail("incomplete directory listing");
}

int main(int argc, char **argv)
{
	char renamed[PATH_MAX], marker[PATH_MAX];
	DIR *dir;
	int fd;

	if (argc != 2)
		fail("usage: rewinddir MOUNTED_DIRECTORY");
	if (snprintf(renamed, sizeof(renamed), "%s.renamed", argv[1]) >= (int)sizeof(renamed) ||
	    snprintf(marker, sizeof(marker), "%s/replacement-marker", argv[1]) >= (int)sizeof(marker))
		fail("test path is too long");
	dir = opendir(argv[1]);
	if (!dir)
		fail("opendir failed");
	check_listing(dir);

	/* Rewinding must follow the open directory after it is renamed. */
	if (rename(argv[1], renamed))
		fail("rename failed");
	rewinddir(dir);
	check_listing(dir);

	/* Replacing the old path must not redirect that same directory stream. */
	if (mkdir(argv[1], 0700))
		fail("mkdir replacement failed");
	fd = open(marker, O_WRONLY | O_CREAT | O_EXCL, 0600);
	if (fd == -1 || close(fd))
		fail("create replacement marker failed");
	rewinddir(dir);
	check_listing(dir);
	if (closedir(dir))
		fail("closedir failed");

	if (unlink(marker) || rmdir(argv[1]) || rename(renamed, argv[1]))
		fail("restore test directory failed");
	return 0;
}
