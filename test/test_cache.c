/*
  Unit tests for cache.c

  cache.c is #included so that its static helpers and the global cache state
  are reachable.  The backend is a small in-memory filesystem that counts the
  calls it gets, and time() is replaced by a clock the tests advance by hand,
  so nothing sleeps and nothing depends on thread timing.  A call that is in
  flight while something else happens is simulated by a hook that the fake
  backend runs after it has computed its reply and before it returns it.

  Output is one TAP-style line per case.  A case listed with an xfail reason
  documents a known bug: it passes the run while the bug reproduces and fails
  the run (XPASS) once it no longer does.  Only the assertions written with
  XCHECK can be expected to fail; any other failed check is a plain failure.
*/

#include <errno.h>
#include <glib.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#include <fuse.h>
#include <fuse_opt.h>
#include "../cache.h"

#define T0 1000000

static time_t test_now = T0;

static time_t test_time(time_t *tp)
{
	if (tp)
		*tp = test_now;
	return test_now;
}

/*
 * Count the strings cache.c gets from g_strndup() that are still allocated,
 * so a missing g_free() shows up in every build and not only under LSan.  The
 * addresses are stored complemented so that LSan does not treat this table as
 * a reference to them.
 */
#define MAX_TRACKED 256
static uintptr_t tracked[MAX_TRACKED];
static int ntracked;

static char *test_strndup(const char *s, gsize n)
{
	char *p = g_strndup(s, n);

	if (ntracked == MAX_TRACKED)
		abort();
	tracked[ntracked++] = ~(uintptr_t) p;
	return p;
}

static void test_free(gpointer p)
{
	int i;

	for (i = 0; i < ntracked; i++) {
		if (tracked[i] == ~(uintptr_t) p) {
			tracked[i] = tracked[--ntracked];
			break;
		}
	}
	g_free(p);
}

#undef g_strndup
#undef g_free
#define g_strndup(s, n) test_strndup(s, n)
#define g_free(p) test_free(p)
#define time(tp) test_time(tp)
#include "../cache.c"
#undef time
#undef g_free
#undef g_strndup

/* ---- harness ---- */

static int cur_fails;		/* failed CHECKs in the running case */
static int cur_xfails;		/* failed XCHECKs in the running case */
static const char *cur_sub;	/* sub-case name, for loops over a table */

static void check_failed(int line, const char *expr, int known)
{
	if (cur_sub)
		printf("#   %s: line %d: %s\n", cur_sub, line, expr);
	else
		printf("#   line %d: %s\n", line, expr);
	if (known)
		cur_xfails++;
	else
		cur_fails++;
}

#define CHECK(cond) \
	do { if (!(cond)) check_failed(__LINE__, #cond, 0); } while (0)
/* the assertion that a known bug makes fail */
#define XCHECK(cond) \
	do { if (!(cond)) check_failed(__LINE__, #cond, 1); } while (0)
/* abandon the case if the setup did not work */
#define REQUIRE(cond) \
	do { if (!(cond)) { check_failed(__LINE__, #cond, 0); return; } } while (0)

#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

/* ---- fake backend: a tiny in-memory filesystem that counts calls ---- */

#define MAX_ENT 32
#define PATH_LEN 64

struct fent {
	int used;
	char path[PATH_LEN];
	mode_t mode;
	off_t size;
	char link[PATH_LEN];
};

static struct fent ents[MAX_ENT];

static struct {
	int init, getattr, readlink, opendir, readdir, releasedir;
	int mknod, mkdir, unlink, rmdir, symlink, rename, link;
	int chmod, chown, utimens, truncate, write, create;
} calls;

static void (*hook_getattr)(void);	/* runs once, inside the next getattr */
static void (*hook_readdir)(void);	/* runs once, inside the next readdir */
static int readdir_err;
static int readdir_no_attrs;
static off_t last_readdir_offset;
static uint64_t last_readdir_fh;
static uint64_t last_releasedir_fh;
static int init_token;

static struct fuse_operations *ops;	/* what cache_wrap() returned */

static struct fent *ent_find(const char *path)
{
	int i;

	for (i = 0; i < MAX_ENT; i++)
		if (ents[i].used && strcmp(ents[i].path, path) == 0)
			return &ents[i];
	return NULL;
}

static struct fent *ent_get(const char *path)
{
	struct fent *e = ent_find(path);

	if (!e) {
		fprintf(stderr, "test bug: no entry %s\n", path);
		abort();
	}
	return e;
}

static struct fent *ent_add(const char *path, mode_t mode, off_t size)
{
	struct fent *e = ent_find(path);
	int i;

	if (!e) {
		for (i = 0; i < MAX_ENT && ents[i].used; i++)
			;
		if (i == MAX_ENT)
			abort();
		e = &ents[i];
	}
	memset(e, 0, sizeof(*e));
	e->used = 1;
	g_strlcpy(e->path, path, sizeof(e->path));
	e->mode = mode;
	e->size = size;
	return e;
}

static void mk_dir(const char *path)
{
	ent_add(path, S_IFDIR | 0755, 0);
}

static void mk_file(const char *path, off_t size)
{
	ent_add(path, S_IFREG | 0644, size);
}

static void mk_link(const char *path, const char *target)
{
	struct fent *e = ent_add(path, S_IFLNK | 0777, 0);

	g_strlcpy(e->link, target, sizeof(e->link));
}

static const char *base_name(const char *path)
{
	const char *s = strrchr(path, '/');

	return s ? s + 1 : path;
}

/* is path a direct child of the directory dir? */
static int is_child(const char *dir, const char *path)
{
	size_t n = strlen(dir);
	const char *rest;

	if (n == 1) {
		rest = path + 1;
		return path[0] == '/' && *rest && !strchr(rest, '/');
	}
	if (strncmp(path, dir, n) != 0 || path[n] != '/')
		return 0;
	rest = path + n + 1;
	return *rest && !strchr(rest, '/');
}

static void fill_stat(struct stat *st, const struct fent *e)
{
	memset(st, 0, sizeof(*st));
	st->st_mode = e->mode;
	st->st_size = e->size;
	st->st_nlink = 1;
}

static void run_hook(void (**hook)(void))
{
	void (*h)(void) = *hook;

	*hook = NULL;
	if (h)
		h();
}

static void *fake_init(struct fuse_conn_info *conn, struct fuse_config *cfg)
{
	(void) conn;
	calls.init++;
	cfg->nullpath_ok = 1;
	return &init_token;
}

static int fake_getattr(const char *path, struct stat *st,
			struct fuse_file_info *fi)
{
	const struct fent *e;

	(void) fi;
	calls.getattr++;
	e = ent_find(path);
	if (!e)
		return -ENOENT;
	fill_stat(st, e);
	/* the reply is computed; anything that happens now comes too late */
	run_hook(&hook_getattr);
	return 0;
}

static int fake_readlink(const char *path, char *buf, size_t size)
{
	const struct fent *e;
	size_t n;

	calls.readlink++;
	e = ent_find(path);
	if (!e || !S_ISLNK(e->mode))
		return -EINVAL;
	n = strlen(e->link);
	if (n > size - 1)
		n = size - 1;
	memcpy(buf, e->link, n);
	buf[n] = '\0';
	return 0;
}

static int fake_opendir(const char *path, struct fuse_file_info *fi)
{
	(void) path;
	calls.opendir++;
	fi->fh = 42;
	return 0;
}

static int fake_releasedir(const char *path, struct fuse_file_info *fi)
{
	(void) path;
	calls.releasedir++;
	last_releasedir_fh = fi->fh;
	return 0;
}

static int fake_readdir(const char *path, void *buf, fuse_fill_dir_t filler,
			off_t offset, struct fuse_file_info *fi,
			enum fuse_readdir_flags flags)
{
	struct fent snap[MAX_ENT];
	int i, n = 0;

	(void) flags;
	calls.readdir++;
	last_readdir_offset = offset;
	last_readdir_fh = fi->fh;
	if (readdir_err)
		return readdir_err;
	if (!ent_find(path))
		return -ENOENT;
	for (i = 0; i < MAX_ENT; i++)
		if (ents[i].used && is_child(path, ents[i].path))
			snap[n++] = ents[i];
	/* the listing is taken; changes from here on are not in it */
	run_hook(&hook_readdir);
	for (i = 0; i < n; i++) {
		struct stat st;

		memset(&st, 0, sizeof(st));
		if (!readdir_no_attrs)
			fill_stat(&st, &snap[i]);
		if (filler(buf, base_name(snap[i].path), &st, 0, 0))
			break;
	}
	return 0;
}

static int fake_mknod(const char *path, mode_t mode, dev_t rdev)
{
	(void) rdev;
	calls.mknod++;
	if (ent_find(path))
		return -EEXIST;
	ent_add(path, mode, 0);
	return 0;
}

static int fake_mkdir(const char *path, mode_t mode)
{
	calls.mkdir++;
	if (ent_find(path))
		return -EEXIST;
	ent_add(path, S_IFDIR | mode, 0);
	return 0;
}

static int fake_unlink(const char *path)
{
	struct fent *e;

	calls.unlink++;
	e = ent_find(path);
	if (!e)
		return -ENOENT;
	e->used = 0;
	return 0;
}

static int fake_rmdir(const char *path)
{
	struct fent *e;

	calls.rmdir++;
	e = ent_find(path);
	if (!e)
		return -ENOENT;
	e->used = 0;
	return 0;
}

static int fake_symlink(const char *from, const char *to)
{
	calls.symlink++;
	if (ent_find(to))
		return -EEXIST;
	mk_link(to, from);
	return 0;
}

static int fake_rename(const char *from, const char *to, unsigned int flags)
{
	struct fent *old;
	size_t n = strlen(from);
	int i;

	(void) flags;
	calls.rename++;
	if (!ent_find(from))
		return -ENOENT;
	old = ent_find(to);
	if (old)
		old->used = 0;
	for (i = 0; i < MAX_ENT; i++) {
		struct fent *e = &ents[i];
		char *np;

		if (!e->used || strncmp(e->path, from, n) != 0 ||
		    (e->path[n] != '\0' && e->path[n] != '/'))
			continue;
		np = g_strdup_printf("%s%s", to, e->path + n);
		g_strlcpy(e->path, np, sizeof(e->path));
		g_free(np);
	}
	return 0;
}

static int fake_link(const char *from, const char *to)
{
	const struct fent *e;

	calls.link++;
	e = ent_find(from);
	if (!e)
		return -ENOENT;
	if (ent_find(to))
		return -EEXIST;
	ent_add(to, e->mode, e->size);
	return 0;
}

static int fake_chmod(const char *path, mode_t mode, struct fuse_file_info *fi)
{
	struct fent *e;

	(void) fi;
	calls.chmod++;
	e = ent_find(path);
	if (!e)
		return -ENOENT;
	e->mode = (e->mode & S_IFMT) | (mode & 07777);
	return 0;
}

static int fake_chown(const char *path, uid_t uid, gid_t gid,
		      struct fuse_file_info *fi)
{
	(void) uid;
	(void) gid;
	(void) fi;
	calls.chown++;
	return ent_find(path) ? 0 : -ENOENT;
}

static int fake_utimens(const char *path, const struct timespec tv[2],
			struct fuse_file_info *fi)
{
	(void) tv;
	(void) fi;
	calls.utimens++;
	return ent_find(path) ? 0 : -ENOENT;
}

static int fake_truncate(const char *path, off_t size,
			 struct fuse_file_info *fi)
{
	struct fent *e;

	(void) fi;
	calls.truncate++;
	e = ent_find(path);
	if (!e)
		return -ENOENT;
	e->size = size;
	return 0;
}

static int fake_write(const char *path, const char *buf, size_t size,
		      off_t offset, struct fuse_file_info *fi)
{
	struct fent *e;

	(void) buf;
	(void) fi;
	calls.write++;
	e = ent_find(path);
	if (!e)
		return -ENOENT;
	if (offset + (off_t) size > e->size)
		e->size = offset + (off_t) size;
	return (int) size;
}

static int fake_create(const char *path, mode_t mode,
		       struct fuse_file_info *fi)
{
	(void) fi;
	calls.create++;
	if (ent_find(path))
		return -EEXIST;
	ent_add(path, S_IFREG | mode, 0);
	return 0;
}

static int fake_access(const char *path, int mask)
{
	(void) path;
	(void) mask;
	return 0;
}

static struct fuse_operations fake_ops = {
	.init = fake_init,
	.getattr = fake_getattr,
	.readlink = fake_readlink,
	.opendir = fake_opendir,
	.releasedir = fake_releasedir,
	.readdir = fake_readdir,
	.mknod = fake_mknod,
	.mkdir = fake_mkdir,
	.unlink = fake_unlink,
	.rmdir = fake_rmdir,
	.symlink = fake_symlink,
	.rename = fake_rename,
	.link = fake_link,
	.chmod = fake_chmod,
	.chown = fake_chown,
	.utimens = fake_utimens,
	.truncate = fake_truncate,
	.write = fake_write,
	.create = fake_create,
	.access = fake_access,
};

/* ---- helpers for the tests ---- */

static void advance(time_t secs)
{
	test_now += secs;
}

static void reset_state(void)
{
	g_hash_table_remove_all(cache.table);
	memset(ents, 0, sizeof(ents));
	memset(&calls, 0, sizeof(calls));
	hook_getattr = NULL;
	hook_readdir = NULL;
	readdir_err = 0;
	readdir_no_attrs = 0;
	last_readdir_offset = -1;
	last_readdir_fh = 0;
	last_releasedir_fh = 0;
	test_now = T0;
	cache.stat_timeout_secs = 20;
	cache.dir_timeout_secs = 20;
	cache.link_timeout_secs = 20;
	cache.max_size = 10000;
	cache.clean_interval_secs = 60;
	cache.min_clean_interval_secs = 5;
	cache.last_cleaned = T0;
	cache.write_ctr = 0;
	mk_dir("/");
}

struct listing {
	int n;
	char name[MAX_ENT][32];
	off_t max_off;
};

static int collect(void *buf, const char *name, const struct stat *st,
		   off_t off, enum fuse_fill_dir_flags flags)
{
	struct listing *l = buf;

	(void) st;
	(void) flags;
	if (l->n < MAX_ENT)
		g_strlcpy(l->name[l->n], name, sizeof(l->name[0]));
	l->n++;
	if (off > l->max_off)
		l->max_off = off;
	return 0;
}

static int listing_has(const struct listing *l, const char *name)
{
	int i;

	for (i = 0; i < l->n && i < MAX_ENT; i++)
		if (strcmp(l->name[i], name) == 0)
			return 1;
	return 0;
}

/* list a directory the way the kernel does: open, read, release */
static int do_readdir(const char *path, off_t offset, struct listing *l)
{
	struct fuse_file_info fi;
	uint64_t dh;
	int err, rel;

	memset(l, 0, sizeof(*l));
	memset(&fi, 0, sizeof(fi));
	err = ops->opendir(path, &fi);
	if (err)
		return err;
	dh = fi.fh;
	err = ops->readdir(path, l, collect, offset, &fi, 0);
	fi.fh = dh;
	rel = ops->releasedir(path, &fi);
	return err ? err : rel;
}

static int do_getattr(const char *path, struct stat *st)
{
	memset(st, 0, sizeof(*st));
	return ops->getattr(path, st, NULL);
}

/* did looking up path go to the backend? */
static int probe(const char *path)
{
	struct stat st;
	int before = calls.getattr;

	(void) do_getattr(path, &st);
	return calls.getattr != before;
}

/* did listing path go to the backend? */
static int probe_dir(const char *path)
{
	struct listing l;
	int before = calls.readdir;

	(void) do_readdir(path, 0, &l);
	return calls.readdir != before;
}

/* ---- getattr ---- */

static void test_getattr_timeout(void)
{
	struct stat st;

	cache.stat_timeout_secs = 10;
	mk_file("/f", 100);

	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(st.st_size == 100);
	CHECK(calls.getattr == 1);

	/* the backend changes behind the cache's back: the old value is served */
	ent_get("/f")->size = 200;
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(st.st_size == 100);
	CHECK(calls.getattr == 1);

	/* still valid during the last second of the timeout */
	advance(10);
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(st.st_size == 100);
	CHECK(calls.getattr == 1);

	advance(1);
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(st.st_size == 200);
	CHECK(calls.getattr == 2);

	/* and the refreshed value is cached again */
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(st.st_size == 200);
	CHECK(calls.getattr == 2);
}

static void test_getattr_errors(void)
{
	struct stat st;

	CHECK(do_getattr("/missing", &st) == -ENOENT);
	CHECK(do_getattr("/missing", &st) == -ENOENT);
	CHECK(calls.getattr == 2);

	/* a file that appears later is seen at once */
	mk_file("/missing", 3);
	REQUIRE(do_getattr("/missing", &st) == 0);
	CHECK(st.st_size == 3);
}

static void test_timeouts_per_kind(void)
{
	struct listing l;
	struct stat st;
	char buf[64];

	cache.stat_timeout_secs = 5;
	cache.dir_timeout_secs = 30;
	cache.link_timeout_secs = 15;
	mk_dir("/d");
	mk_file("/d/a", 1);
	mk_link("/d/l", "target");

	REQUIRE(do_readdir("/d", 0, &l) == 0);
	REQUIRE(ops->readlink("/d/l", buf, sizeof(buf)) == 0);

	advance(10);		/* T0+10: attributes expired, the rest not */
	CHECK(probe("/d/a"));
	CHECK(ops->readlink("/d/l", buf, sizeof(buf)) == 0);
	CHECK(calls.readlink == 1);
	CHECK(do_readdir("/d", 0, &l) == 0);
	CHECK(calls.readdir == 1);

	advance(10);		/* T0+20: link expired too */
	CHECK(ops->readlink("/d/l", buf, sizeof(buf)) == 0);
	CHECK(calls.readlink == 2);
	CHECK(do_readdir("/d", 0, &l) == 0);
	CHECK(calls.readdir == 1);

	advance(11);		/* T0+31: listing expired */
	CHECK(do_readdir("/d", 0, &l) == 0);
	CHECK(calls.readdir == 2);
	CHECK(do_getattr("/d/a", &st) == 0);
}

/* ---- invalidation by modifying operations ---- */

static int op_mknod(void)
{
	return ops->mknod("/d/new", S_IFREG | 0644, 0);
}

static int op_mkdir(void)
{
	return ops->mkdir("/d/new", 0755);
}

static int op_create(void)
{
	struct fuse_file_info fi;

	memset(&fi, 0, sizeof(fi));
	return ops->create("/d/new", 0644, &fi);
}

static int op_symlink(void)
{
	return ops->symlink("target", "/d/new");
}

static int op_link(void)
{
	return ops->link("/d/f", "/d/new");
}

static int op_unlink(void)
{
	return ops->unlink("/d/f");
}

static int op_rmdir(void)
{
	return ops->rmdir("/d/sub");
}

static int op_rename(void)
{
	return ops->rename("/d/f", "/d/h", 0);
}

static int op_chmod(void)
{
	return ops->chmod("/d/f", 0600, NULL);
}

static int op_chown(void)
{
	return ops->chown("/d/f", 1000, 1000, NULL);
}

static int op_utimens(void)
{
	struct timespec tv[2];

	memset(tv, 0, sizeof(tv));
	return ops->utimens("/d/f", tv, NULL);
}

static int op_truncate(void)
{
	return ops->truncate("/d/f", 0, NULL);
}

static int op_write(void)
{
	return ops->write("/d/f", "x", 1, 0, NULL) == 1 ? 0 : -1;
}

static const struct inval_case {
	const char *name;
	int (*op)(void);
	const char *target;	/* cached path that must be refetched, if any */
	int purges_parent;	/* the listing of /d must be refetched */
} inval_cases[] = {
	{ "mknod",    op_mknod,    NULL,     1 },
	{ "mkdir",    op_mkdir,    NULL,     1 },
	{ "create",   op_create,   NULL,     1 },
	{ "symlink",  op_symlink,  NULL,     1 },
	{ "link",     op_link,     "/d/f",   1 },
	{ "unlink",   op_unlink,   "/d/f",   1 },
	{ "rmdir",    op_rmdir,    "/d/sub", 1 },
	{ "rename",   op_rename,   "/d/f",   1 },
	{ "chmod",    op_chmod,    "/d/f",   0 },
	{ "chown",    op_chown,    "/d/f",   0 },
	{ "utimens",  op_utimens,  "/d/f",   0 },
	{ "truncate", op_truncate, "/d/f",   0 },
	{ "write",    op_write,    "/d/f",   0 },
};

static void test_invalidation(void)
{
	struct listing l;
	size_t i;

	for (i = 0; i < ARRAY_SIZE(inval_cases); i++) {
		const struct inval_case *c = &inval_cases[i];

		cur_sub = c->name;
		reset_state();
		mk_dir("/d");
		mk_file("/d/f", 1);
		mk_file("/d/g", 2);
		mk_dir("/d/sub");
		mk_file("/other", 3);

		/* the listing of /d also caches the attributes of its entries */
		REQUIRE(do_readdir("/d", 0, &l) == 0);
		(void) probe("/other");
		REQUIRE(!probe("/d/g"));
		REQUIRE(!probe_dir("/d"));
		if (c->target)
			REQUIRE(!probe(c->target));

		CHECK(c->op() == 0);

		if (c->target)
			CHECK(probe(c->target));
		if (c->purges_parent)
			CHECK(probe_dir("/d"));
		/* other entries are left alone */
		CHECK(!probe("/d/g"));
		CHECK(!probe("/other"));
	}
	cur_sub = NULL;
}

static void test_invalidation_fresh_values(void)
{
	struct stat st;

	mk_file("/f", 100);
	REQUIRE(do_getattr("/f", &st) == 0);

	REQUIRE(ops->chmod("/f", 0600, NULL) == 0);
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK((st.st_mode & 07777) == 0600);

	REQUIRE(ops->truncate("/f", 7, NULL) == 0);
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(st.st_size == 7);

	REQUIRE(ops->write("/f", "abc", 3, 50, NULL) == 3);
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(st.st_size == 53);

	CHECK(calls.getattr == 4);
}

static void test_cache_invalidate(void)
{
	mk_file("/f", 1);
	mk_file("/g", 1);
	(void) probe("/f");
	(void) probe("/g");
	CHECK(!probe("/f"));

	cache_invalidate("/f");
	CHECK(probe("/f"));
	CHECK(!probe("/g"));
}

static void test_rename_moves_children(void)
{
	struct listing l;
	struct stat st;

	mk_dir("/a");
	mk_file("/a/f", 1);
	mk_dir("/a/sub");
	mk_file("/a/sub/g", 2);
	mk_file("/zz", 3);
	REQUIRE(do_readdir("/", 0, &l) == 0);
	REQUIRE(do_readdir("/a", 0, &l) == 0);
	(void) probe("/a");
	(void) probe("/a/sub");
	(void) probe("/a/sub/g");
	REQUIRE(!probe("/a/f"));
	REQUIRE(!probe("/a/sub/g"));
	REQUIRE(!probe_dir("/"));
	REQUIRE(!probe_dir("/a"));

	REQUIRE(ops->rename("/a", "/b", 0) == 0);

	/* nothing under the old name is served any more */
	CHECK(do_getattr("/a", &st) == -ENOENT);
	CHECK(do_getattr("/a/f", &st) == -ENOENT);
	CHECK(do_getattr("/a/sub", &st) == -ENOENT);
	CHECK(do_getattr("/a/sub/g", &st) == -ENOENT);
	CHECK(do_readdir("/a", 0, &l) == -ENOENT);

	/* the children show up under the new name */
	CHECK(do_getattr("/b/f", &st) == 0);
	CHECK(do_getattr("/b/sub/g", &st) == 0);
	CHECK(st.st_size == 2);
	CHECK(do_readdir("/b", 0, &l) == 0);
	CHECK(listing_has(&l, "f"));
	CHECK(listing_has(&l, "sub"));

	/* the listing of the parent was refetched */
	CHECK(do_readdir("/", 0, &l) == 0);
	CHECK(listing_has(&l, "b"));
	CHECK(!listing_has(&l, "a"));
	CHECK(listing_has(&l, "zz"));

	/* an unrelated entry is still cached */
	CHECK(!probe("/zz"));
}

static void test_rename_nested(void)
{
	struct listing l;
	struct stat st;

	mk_dir("/d");
	mk_dir("/d/sub");
	mk_file("/d/f", 4);
	REQUIRE(do_readdir("/d", 0, &l) == 0);
	REQUIRE(do_readdir("/d/sub", 0, &l) == 0);

	REQUIRE(ops->rename("/d/f", "/d/sub/f2", 0) == 0);

	/* both parents were purged */
	CHECK(do_readdir("/d", 0, &l) == 0);
	CHECK(!listing_has(&l, "f"));
	CHECK(do_readdir("/d/sub", 0, &l) == 0);
	CHECK(listing_has(&l, "f2"));
	CHECK(do_getattr("/d/f", &st) == -ENOENT);
	CHECK(do_getattr("/d/sub/f2", &st) == 0);
}

static void test_rename_over_existing(void)
{
	struct stat st;

	mk_file("/x", 3);
	mk_file("/y", 7);
	REQUIRE(do_getattr("/x", &st) == 0);
	REQUIRE(do_getattr("/y", &st) == 0);
	CHECK(st.st_size == 7);

	REQUIRE(ops->rename("/x", "/y", 0) == 0);

	CHECK(do_getattr("/y", &st) == 0);
	CHECK(st.st_size == 3);
	CHECK(do_getattr("/x", &st) == -ENOENT);
}

static void test_failed_ops_keep_cache(void)
{
	struct listing l;
	uint64_t w;

	mk_dir("/d");
	mk_file("/d/f", 1);
	REQUIRE(do_readdir("/d", 0, &l) == 0);
	w = cache_get_write_ctr();

	CHECK(ops->mkdir("/d/f", 0755) == -EEXIST);
	CHECK(ops->unlink("/d/none") == -ENOENT);
	CHECK(ops->rmdir("/d/none") == -ENOENT);
	CHECK(ops->chmod("/d/none", 0600, NULL) == -ENOENT);
	CHECK(ops->truncate("/d/none", 0, NULL) == -ENOENT);
	CHECK(ops->write("/d/none", "x", 1, 0, NULL) == -ENOENT);
	CHECK(ops->rename("/d/none", "/d/f", 0) == -ENOENT);

	/* a write that failed does not count as one */
	CHECK(cache_get_write_ctr() == w);
	CHECK(!probe("/d/f"));
	CHECK(!probe_dir("/d"));
}

/* ---- readdir ---- */

static void test_readdir_cached(void)
{
	struct listing l;
	struct stat st;

	mk_dir("/d");
	mk_file("/d/a", 10);
	mk_file("/d/b", 20);
	mk_dir("/d/c");

	REQUIRE(do_readdir("/d", 0, &l) == 0);
	CHECK(l.n == 3);
	CHECK(listing_has(&l, "a") && listing_has(&l, "b") &&
	      listing_has(&l, "c"));
	CHECK(calls.readdir == 1);
	CHECK(calls.opendir == 1);
	CHECK(calls.releasedir == 1);
	CHECK(last_readdir_offset == 0);

	/* the listing also cached the attributes of the entries */
	REQUIRE(do_getattr("/d/a", &st) == 0);
	CHECK(st.st_size == 10);
	CHECK(calls.getattr == 0);

	/* a second listing is served from the cache, without opening the
	   directory on the backend */
	mk_file("/d/late", 1);
	REQUIRE(do_readdir("/d", 0, &l) == 0);
	CHECK(l.n == 3);
	CHECK(!listing_has(&l, "late"));
	CHECK(calls.readdir == 1);
	CHECK(calls.opendir == 1);
	CHECK(calls.releasedir == 1);

	/* valid through the last second of dir_timeout */
	advance(20);
	REQUIRE(do_readdir("/d", 0, &l) == 0);
	CHECK(l.n == 3);
	CHECK(calls.readdir == 1);

	advance(1);
	REQUIRE(do_readdir("/d", 0, &l) == 0);
	CHECK(l.n == 4);
	CHECK(listing_has(&l, "late"));
	CHECK(calls.readdir == 2);
	CHECK(calls.opendir == 2);
	CHECK(calls.releasedir == 2);
}

/*
 * sshfs hands the whole listing to the filler with offset 0 and leaves the
 * slicing to libfuse, so an offset from the caller must neither cut the
 * listing short nor be passed on (upstream issues 338 and 379).
 */
static void test_readdir_ignores_offset(void)
{
	struct listing l;

	mk_dir("/d");
	mk_file("/d/a", 1);
	mk_file("/d/b", 2);
	mk_file("/d/c", 3);

	/* cold cache */
	REQUIRE(do_readdir("/d", 2, &l) == 0);
	CHECK(l.n == 3);
	CHECK(last_readdir_offset == 0);
	CHECK(l.max_off == 0);
	CHECK(calls.readdir == 1);

	/* warm cache, new handle, other offsets */
	REQUIRE(do_readdir("/d", 3, &l) == 0);
	CHECK(l.n == 3);
	CHECK(l.max_off == 0);
	REQUIRE(do_readdir("/d", 1000, &l) == 0);
	CHECK(l.n == 3);
	CHECK(calls.readdir == 1);

	/* expired, nonzero offset again */
	advance(21);
	REQUIRE(do_readdir("/d", 5, &l) == 0);
	CHECK(l.n == 3);
	CHECK(last_readdir_offset == 0);
	CHECK(calls.readdir == 2);
}

static void test_readdir_root(void)
{
	struct listing l;
	struct stat st;

	mk_file("/x", 5);
	mk_dir("/d");

	REQUIRE(do_readdir("/", 0, &l) == 0);
	CHECK(l.n == 2);
	CHECK(listing_has(&l, "x") && listing_has(&l, "d"));

	/* entries of the root are cached as /x, not //x */
	REQUIRE(do_getattr("/x", &st) == 0);
	CHECK(st.st_size == 5);
	CHECK(calls.getattr == 0);
}

static void test_readdir_without_attrs(void)
{
	struct listing l;
	struct stat st;

	readdir_no_attrs = 1;
	mk_dir("/d");
	mk_file("/d/a", 5);

	REQUIRE(do_readdir("/d", 0, &l) == 0);
	CHECK(listing_has(&l, "a"));

	/* an entry listed without attributes has to be looked up */
	REQUIRE(do_getattr("/d/a", &st) == 0);
	CHECK(st.st_size == 5);
	CHECK(calls.getattr == 1);

	CHECK(!probe_dir("/d"));
}

static void test_readdir_error_not_cached(void)
{
	struct listing l;

	mk_dir("/d");
	mk_file("/d/a", 1);

	readdir_err = -EIO;
	CHECK(do_readdir("/d", 0, &l) == -EIO);
	readdir_err = 0;
	REQUIRE(do_readdir("/d", 0, &l) == 0);
	CHECK(listing_has(&l, "a"));
	CHECK(calls.readdir == 2);
}

static void test_readdir_handle_reuse(void)
{
	struct fuse_file_info fi;
	struct listing l;
	uint64_t dh;

	mk_dir("/d");
	mk_file("/d/a", 1);
	memset(&fi, 0, sizeof(fi));
	REQUIRE(ops->opendir("/d", &fi) == 0);
	dh = fi.fh;
	CHECK(calls.opendir == 0);

	memset(&l, 0, sizeof(l));
	fi.fh = dh;
	CHECK(ops->readdir("/d", &l, collect, 0, &fi, 0) == 0);
	CHECK(calls.opendir == 1);
	CHECK(last_readdir_fh == 42);

	/* a second backend listing through the same handle reuses the
	   backend handle */
	advance(cache.dir_timeout_secs + 1);
	memset(&l, 0, sizeof(l));
	fi.fh = dh;
	CHECK(ops->readdir("/d", &l, collect, 0, &fi, 0) == 0);
	CHECK(calls.readdir == 2);
	CHECK(calls.opendir == 1);
	CHECK(last_readdir_fh == 42);

	fi.fh = dh;
	CHECK(ops->releasedir("/d", &fi) == 0);
	CHECK(calls.releasedir == 1);
	CHECK(last_releasedir_fh == 42);
}

static void test_readdir_attr_independent(void)
{
	struct listing l;
	struct stat st;

	mk_dir("/d");
	mk_file("/d/a", 1);
	mk_dir("/e");

	/* a listing says nothing about the attributes of the directory */
	REQUIRE(do_readdir("/d", 0, &l) == 0);
	REQUIRE(do_getattr("/d", &st) == 0);
	CHECK(calls.getattr == 1);
	/* and caching them leaves the listing alone */
	CHECK(!probe_dir("/d"));

	/* attributes alone do not make a listing either */
	REQUIRE(do_getattr("/e", &st) == 0);
	CHECK(probe_dir("/e"));
}

/* ---- readlink ---- */

static void test_readlink_cached(void)
{
	char buf[64];

	cache.link_timeout_secs = 10;
	mk_link("/l", "target-1");

	REQUIRE(ops->readlink("/l", buf, sizeof(buf)) == 0);
	CHECK(strcmp(buf, "target-1") == 0);
	CHECK(calls.readlink == 1);

	g_strlcpy(ent_get("/l")->link, "target-2", PATH_LEN);
	advance(10);
	REQUIRE(ops->readlink("/l", buf, sizeof(buf)) == 0);
	CHECK(strcmp(buf, "target-1") == 0);
	CHECK(calls.readlink == 1);

	advance(1);
	REQUIRE(ops->readlink("/l", buf, sizeof(buf)) == 0);
	CHECK(strcmp(buf, "target-2") == 0);
	CHECK(calls.readlink == 2);

	/* cached attributes of a path do not stand in for its target */
	mk_link("/m", "other");
	(void) probe("/m");
	REQUIRE(ops->readlink("/m", buf, sizeof(buf)) == 0);
	CHECK(strcmp(buf, "other") == 0);
	CHECK(calls.readlink == 3);
}

static void test_readlink_truncation(void)
{
	char buf[8];
	char small[5];
	char one[1];

	mk_link("/l", "0123456789");

	/* the backend fills a buffer of 5 and the cache keeps what fit */
	memset(small, 'X', sizeof(small));
	REQUIRE(ops->readlink("/l", small, sizeof(small)) == 0);
	CHECK(strcmp(small, "0123") == 0);
	CHECK(calls.readlink == 1);

	memset(small, 'X', sizeof(small));
	REQUIRE(ops->readlink("/l", small, sizeof(small)) == 0);
	CHECK(strcmp(small, "0123") == 0);
	CHECK(calls.readlink == 1);

	/* a smaller buffer gets a shorter, terminated string from the cache */
	memset(buf, 'X', sizeof(buf));
	REQUIRE(ops->readlink("/l", buf, 3) == 0);
	CHECK(strcmp(buf, "01") == 0);
	CHECK(buf[3] == 'X');
	CHECK(calls.readlink == 1);

	/* a buffer of one byte holds just the terminator */
	cache_invalidate("/l");
	one[0] = 'X';
	REQUIRE(ops->readlink("/l", one, sizeof(one)) == 0);
	CHECK(one[0] == '\0');
	one[0] = 'X';
	REQUIRE(ops->readlink("/l", one, sizeof(one)) == 0);
	CHECK(one[0] == '\0');
	CHECK(calls.readlink == 2);
}

static void test_readlink_freed(void)
{
	char buf[64];

	mk_link("/l", "target");
	CHECK(ntracked == 0);
	REQUIRE(ops->readlink("/l", buf, sizeof(buf)) == 0);
	CHECK(ntracked == 1);

	/* refreshing an expired link frees the old string */
	advance(cache.link_timeout_secs + 1);
	REQUIRE(ops->readlink("/l", buf, sizeof(buf)) == 0);
	CHECK(calls.readlink == 2);
	CHECK(ntracked == 1);

	/* dropping the node frees the string (free_node) */
	cache_invalidate("/l");
	CHECK(ntracked == 0);

	REQUIRE(ops->readlink("/l", buf, sizeof(buf)) == 0);
	CHECK(ntracked == 1);
	g_hash_table_remove_all(cache.table);
	CHECK(ntracked == 0);
}

/* ---- the write counter ---- */

static void hook_write_f(void)
{
	(void) ops->write("/f", "hello", 5, 100, NULL);
}

static void hook_write_da(void)
{
	(void) ops->write("/d/a", "hello", 5, 100, NULL);
}

static void test_write_guard_getattr(void)
{
	struct stat st;
	uint64_t w;

	mk_file("/f", 10);
	hook_getattr = hook_write_f;
	w = cache_get_write_ctr();

	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(st.st_size == 10);	/* the reply that was in flight */
	CHECK(cache_get_write_ctr() == w + 1);
	CHECK(cache_lookup("/f") == NULL);

	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(calls.getattr == 2);
	CHECK(st.st_size == 105);
}

static void test_write_guard_control(void)
{
	struct stat st;

	mk_file("/f", 10);
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(cache_lookup("/f") != NULL);
	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK(calls.getattr == 1);
}

static void test_write_guard_api(void)
{
	struct stat st, st2;
	uint64_t w;

	mk_file("/f", 1);
	memset(&st, 0, sizeof(st));
	st.st_mode = S_IFREG | 0644;
	st.st_size = 1;

	w = cache_get_write_ctr();
	REQUIRE(ops->write("/f", "x", 1, 0, NULL) == 1);
	cache_add_attr("/f", &st, w);
	CHECK(cache_lookup("/f") == NULL);

	cache_add_attr("/f", &st, cache_get_write_ctr());
	CHECK(cache_lookup("/f") != NULL);
	REQUIRE(do_getattr("/f", &st2) == 0);
	CHECK(calls.getattr == 0);
}

static void test_write_guard_readdir(void)
{
	struct listing l;
	struct stat st;

	mk_dir("/d");
	mk_file("/d/a", 1);
	mk_file("/d/b", 2);
	hook_readdir = hook_write_da;

	REQUIRE(do_readdir("/d", 0, &l) == 0);
	CHECK(l.n == 2);

	/* the attributes that came with the listing may predate the write */
	CHECK(cache_lookup("/d/a") == NULL);
	CHECK(cache_lookup("/d/b") == NULL);
	REQUIRE(do_getattr("/d/a", &st) == 0);
	CHECK(st.st_size == 105);
}

/* ---- calls in flight across operations that do not bump write_ctr ---- */

static void hook_mkdir_new(void)
{
	(void) ops->mkdir("/d/new", 0755);
}

static void hook_chmod_f(void)
{
	(void) ops->chmod("/f", 0600, NULL);
}

static void hook_chmod_da(void)
{
	(void) ops->chmod("/d/a", 0600, NULL);
}

/*
 * Only cache_write() advances write_ctr.  The other modifying operations
 * just purge, so a lookup that started before one of them can still install
 * what it read afterwards, and the stale value is then served until its
 * timeout.
 */
static void test_race_readdir_mkdir(void)
{
	struct listing l;

	mk_dir("/d");
	mk_file("/d/a", 1);
	mk_file("/d/b", 2);
	hook_readdir = hook_mkdir_new;

	REQUIRE(do_readdir("/d", 0, &l) == 0);
	CHECK(l.n == 2);	/* the listing in flight predates the mkdir */
	REQUIRE(ent_find("/d/new") != NULL);

	REQUIRE(do_readdir("/d", 0, &l) == 0);
	XCHECK(listing_has(&l, "new"));
}

static void test_race_getattr_chmod(void)
{
	struct stat st;

	mk_file("/f", 1);
	hook_getattr = hook_chmod_f;

	REQUIRE(do_getattr("/f", &st) == 0);
	CHECK((st.st_mode & 07777) == 0644);	/* the reply in flight */
	REQUIRE((ent_get("/f")->mode & 07777) == 0600);

	REQUIRE(do_getattr("/f", &st) == 0);
	XCHECK((st.st_mode & 07777) == 0600);
}

static void test_race_readdir_chmod(void)
{
	struct listing l;
	struct stat st;

	mk_dir("/d");
	mk_file("/d/a", 1);
	hook_readdir = hook_chmod_da;

	REQUIRE(do_readdir("/d", 0, &l) == 0);
	REQUIRE((ent_get("/d/a")->mode & 07777) == 0600);

	REQUIRE(do_getattr("/d/a", &st) == 0);
	XCHECK((st.st_mode & 07777) == 0600);
}

/* ---- cleaning ---- */

static void test_clean_interval(void)
{
	cache.stat_timeout_secs = 10;
	cache.clean_interval_secs = 60;
	cache.min_clean_interval_secs = 5;
	mk_file("/a", 1);
	mk_file("/b", 1);
	mk_file("/c", 1);
	mk_file("/d", 1);

	(void) probe("/a");
	(void) probe("/b");		/* valid until T0+10 */
	advance(55);
	(void) probe("/c");		/* valid until T0+65 */
	/* /a and /b are expired but nothing cleans before the interval */
	CHECK(g_hash_table_size(cache.table) == 3);

	advance(6);			/* T0+61 */
	(void) probe("/d");
	CHECK(g_hash_table_size(cache.table) == 2);
	CHECK(cache_lookup("/a") == NULL);
	CHECK(cache_lookup("/b") == NULL);
	CHECK(cache_lookup("/c") != NULL);
	CHECK(cache_lookup("/d") != NULL);
	CHECK(cache.last_cleaned == T0 + 61);

	/* what stayed is still served from the cache */
	CHECK(!probe("/c"));
}

static void test_clean_by_size(void)
{
	cache.stat_timeout_secs = 4;
	cache.max_size = 3;
	cache.clean_interval_secs = 1000;
	cache.min_clean_interval_secs = 5;
	mk_file("/a", 1);
	mk_file("/b", 1);
	mk_file("/c", 1);
	mk_file("/d", 1);
	mk_file("/e", 1);

	(void) probe("/a");
	(void) probe("/b");
	(void) probe("/c");
	(void) probe("/d");
	/* over the limit, but the minimum interval has not passed */
	CHECK(g_hash_table_size(cache.table) == 4);

	advance(6);
	(void) probe("/e");
	CHECK(g_hash_table_size(cache.table) == 1);
	CHECK(cache_lookup("/a") == NULL);
	CHECK(cache_lookup("/e") != NULL);
}

static void test_clean_keeps_valid_entries(void)
{
	cache.stat_timeout_secs = 100;
	cache.max_size = 2;
	cache.clean_interval_secs = 1000;
	cache.min_clean_interval_secs = 5;
	mk_file("/a", 1);
	mk_file("/b", 1);
	mk_file("/c", 1);
	mk_file("/d", 1);

	(void) probe("/a");
	(void) probe("/b");
	(void) probe("/c");
	advance(6);
	(void) probe("/d");

	/* the limit is soft: a clean ran, but nothing in there had expired */
	CHECK(cache.last_cleaned == T0 + 6);
	CHECK(g_hash_table_size(cache.table) == 4);
	CHECK(!probe("/a"));
}

static void test_clean_keeps_node_with_valid_part(void)
{
	struct listing l;

	cache.stat_timeout_secs = 5;
	cache.dir_timeout_secs = 50;
	cache.clean_interval_secs = 10;
	cache.min_clean_interval_secs = 1;
	mk_dir("/d");
	mk_file("/d/a", 1);
	mk_file("/x", 1);

	REQUIRE(do_readdir("/d", 0, &l) == 0);
	advance(20);
	(void) probe("/x");		/* runs a clean */

	CHECK(cache.last_cleaned == T0 + 20);
	CHECK(cache_lookup("/d/a") == NULL);	/* attributes expired */
	CHECK(cache_lookup("/d") != NULL);	/* listing still valid */
	CHECK(g_hash_table_size(cache.table) == 2);
	CHECK(!probe_dir("/d"));
}

/* ---- setup of the wrapped operations ---- */

static void test_parse_defaults(void)
{
	char a0[] = "sshfs";
	char *argv[] = { a0, NULL };
	struct fuse_args args = FUSE_ARGS_INIT(1, argv);

	REQUIRE(cache_parse_options(&args) == 0);
	CHECK(cache.stat_timeout_secs == 20);
	CHECK(cache.dir_timeout_secs == 20);
	CHECK(cache.link_timeout_secs == 20);
	CHECK(cache.max_size == 10000);
	CHECK(cache.clean_interval_secs == 60);
	CHECK(cache.min_clean_interval_secs == 5);
	CHECK(args.argc == 1);
	fuse_opt_free_args(&args);
}

static void test_parse_options(void)
{
	char a0[] = "sshfs";
	char a1[] = "-o";
	char a2[] = "dcache_timeout=7,dcache_stat_timeout=3,dcache_max_size=50,"
		    "dcache_clean_interval=30,dcache_min_clean_interval=2";
	char *argv[] = { a0, a1, a2, NULL };
	struct fuse_args args = FUSE_ARGS_INIT(3, argv);

	REQUIRE(cache_parse_options(&args) == 0);
	CHECK(cache.stat_timeout_secs == 3);	/* the later, specific one wins */
	CHECK(cache.dir_timeout_secs == 7);
	CHECK(cache.link_timeout_secs == 7);
	CHECK(cache.max_size == 50);
	CHECK(cache.clean_interval_secs == 30);
	CHECK(cache.min_clean_interval_secs == 2);
	CHECK(args.argc == 1);		/* all of them were consumed */
	fuse_opt_free_args(&args);
}

static void test_parse_legacy_options(void)
{
	char a0[] = "sshfs";
	char a1[] = "-o";
	char a2[] = "cache_timeout=9,cache_dir_timeout=4,cache_link_timeout=5,"
		    "cache_max_size=8,cache_clean_interval=31,"
		    "cache_min_clean_interval=3";
	char *argv[] = { a0, a1, a2, NULL };
	struct fuse_args args = FUSE_ARGS_INIT(3, argv);

	REQUIRE(cache_parse_options(&args) == 0);
	CHECK(cache.stat_timeout_secs == 9);
	CHECK(cache.dir_timeout_secs == 4);
	CHECK(cache.link_timeout_secs == 5);
	CHECK(cache.max_size == 8);
	CHECK(cache.clean_interval_secs == 31);
	CHECK(cache.min_clean_interval_secs == 3);
	CHECK(args.argc == 1);
	fuse_opt_free_args(&args);
}

static void test_init(void)
{
	struct fuse_conn_info conn;
	struct fuse_config cfg;
	void *res;

	memset(&conn, 0, sizeof(conn));
	memset(&cfg, 0, sizeof(cfg));
	res = ops->init(&conn, &cfg);
	CHECK(calls.init == 1);
	CHECK(res == &init_token);
	/* the cache needs a path for every request */
	CHECK(cfg.nullpath_ok == 0);
}

static void test_fill(void)
{
	struct fuse_operations minimal, out;

	/* everything the backend has is wrapped, with the cache's own
	   directory handling */
	CHECK(ops->getattr == cache_getattr);
	CHECK(ops->readlink == cache_readlink);
	CHECK(ops->readdir == cache_readdir);
	CHECK(ops->opendir == cache_opendir);
	CHECK(ops->releasedir == cache_releasedir);
	CHECK(ops->init == cache_init);
	CHECK(ops->write == cache_write);
	CHECK(ops->access == fake_access);

	/* what the backend lacks stays missing */
	memset(&minimal, 0, sizeof(minimal));
	memset(&out, 0, sizeof(out));
	minimal.getattr = fake_getattr;
	cache_fill(&minimal, &out);
	CHECK(out.getattr == cache_getattr);
	CHECK(out.opendir == cache_opendir);
	CHECK(out.releasedir == cache_releasedir);
	CHECK(out.readdir == NULL);
	CHECK(out.readlink == NULL);
	CHECK(out.mkdir == NULL);
	CHECK(out.unlink == NULL);
	CHECK(out.rename == NULL);
	CHECK(out.chmod == NULL);
	CHECK(out.write == NULL);
	CHECK(out.create == NULL);
	CHECK(out.access == NULL);
}

/* ---- runner ---- */

struct tcase {
	const char *name;
	void (*fn)(void);
	const char *xfail;	/* why the case is expected to fail, if it is */
};

static const struct tcase cases[] = {
	{ "getattr: cached until stat_timeout", test_getattr_timeout, NULL },
	{ "getattr: errors are not cached", test_getattr_errors, NULL },
	{ "timeouts: stat, dir and link expire separately",
	  test_timeouts_per_kind, NULL },
	{ "invalidate: modifying ops purge the path and its parent",
	  test_invalidation, NULL },
	{ "invalidate: next getattr returns the new value",
	  test_invalidation_fresh_values, NULL },
	{ "invalidate: cache_invalidate drops one path",
	  test_cache_invalidate, NULL },
	{ "rename: children of the old name are dropped",
	  test_rename_moves_children, NULL },
	{ "rename: both parents are purged", test_rename_nested, NULL },
	{ "rename: an existing target is not served stale",
	  test_rename_over_existing, NULL },
	{ "invalidate: failed ops leave the cache alone",
	  test_failed_ops_keep_cache, NULL },
	{ "readdir: cached until dir_timeout, fills child attrs",
	  test_readdir_cached, NULL },
	{ "readdir: offset is ignored", test_readdir_ignores_offset, NULL },
	{ "readdir: root entries are cached as /name", test_readdir_root, NULL },
	{ "readdir: entries without attrs are not attr-cached",
	  test_readdir_without_attrs, NULL },
	{ "readdir: errors are not cached", test_readdir_error_not_cached,
	  NULL },
	{ "readdir: backend handle is opened once per handle",
	  test_readdir_handle_reuse, NULL },
	{ "readdir: listing and attrs of a dir are separate",
	  test_readdir_attr_independent, NULL },
	{ "readlink: cached until link_timeout", test_readlink_cached, NULL },
	{ "readlink: cached target is cut to the buffer size",
	  test_readlink_truncation, NULL },
	{ "readlink: cached target is freed with its node",
	  test_readlink_freed, NULL },
	{ "write counter: getattr in flight across a write is not cached",
	  test_write_guard_getattr, NULL },
	{ "write counter: getattr without a write is cached",
	  test_write_guard_control, NULL },
	{ "write counter: cache_add_attr drops a stale counter",
	  test_write_guard_api, NULL },
	{ "write counter: readdir in flight across a write drops child attrs",
	  test_write_guard_readdir, NULL },
	{ "race: readdir in flight across mkdir caches a stale listing",
	  test_race_readdir_mkdir,
	  "only write bumps write_ctr; mkdir purges before cache_add_dir" },
	{ "race: getattr in flight across chmod caches the old mode",
	  test_race_getattr_chmod,
	  "only write bumps write_ctr; chmod purges before cache_add_attr" },
	{ "race: readdir in flight across chmod caches the old child mode",
	  test_race_readdir_chmod,
	  "only write bumps write_ctr; chmod purges before cache_dirfill" },
	{ "clean: expired entries go after clean_interval",
	  test_clean_interval, NULL },
	{ "clean: expired entries go when over max_size",
	  test_clean_by_size, NULL },
	{ "clean: valid entries are kept over max_size",
	  test_clean_keeps_valid_entries, NULL },
	{ "clean: a node with one valid part is kept",
	  test_clean_keeps_node_with_valid_part, NULL },
	{ "options: defaults", test_parse_defaults, NULL },
	{ "options: dcache_* names", test_parse_options, NULL },
	{ "options: cache_* names", test_parse_legacy_options, NULL },
	{ "init: forwards to the backend and requires paths", test_init, NULL },
	{ "wrap: only existing backend ops are wrapped", test_fill, NULL },
};

int main(void)
{
	int failed = 0;
	size_t i;

	setvbuf(stdout, NULL, _IOLBF, 0);

	ops = cache_wrap(&fake_ops);
	if (!ops) {
		printf("Bail out! cache_wrap failed\n");
		return 1;
	}

	printf("1..%d\n", (int) ARRAY_SIZE(cases));
	for (i = 0; i < ARRAY_SIZE(cases); i++) {
		const struct tcase *tc = &cases[i];
		int n = (int) i + 1;

		reset_state();
		cur_fails = 0;
		cur_xfails = 0;
		cur_sub = NULL;
		ntracked = 0;

		tc->fn();

		/* dropping every node must free everything it holds */
		g_hash_table_remove_all(cache.table);
		if (ntracked) {
			printf("#   %d cached string(s) not freed\n", ntracked);
			cur_fails++;
		}

		if (cur_fails) {
			printf("not ok %d - %s\n", n, tc->name);
			failed++;
		} else if (!tc->xfail) {
			if (cur_xfails) {
				printf("not ok %d - %s\n", n, tc->name);
				failed++;
			} else {
				printf("ok %d - %s\n", n, tc->name);
			}
		} else if (cur_xfails) {
			printf("ok %d - %s # XFAIL %s\n", n, tc->name,
			       tc->xfail);
		} else {
			printf("not ok %d - %s # XPASS %s: no longer fails, "
			       "remove the xfail\n", n, tc->name, tc->xfail);
			failed++;
		}
	}

	g_hash_table_destroy(cache.table);
	cache.table = NULL;
	return failed ? 1 : 0;
}
