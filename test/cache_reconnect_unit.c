#include "cache.h"

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

struct fill_result {
	unsigned int count;
	char last_name[64];
};

struct gate {
	pthread_mutex_t lock;
	pthread_cond_t started_cond;
	pthread_cond_t release_cond;
	int block;
	int started;
	int release;
};

static struct gate readdir_gate = {
	.lock = PTHREAD_MUTEX_INITIALIZER,
	.started_cond = PTHREAD_COND_INITIALIZER,
	.release_cond = PTHREAD_COND_INITIALIZER,
};
static struct gate readlink_gate = {
	.lock = PTHREAD_MUTEX_INITIALIZER,
	.started_cond = PTHREAD_COND_INITIALIZER,
	.release_cond = PTHREAD_COND_INITIALIZER,
};

static struct fuse_operations *cache_oper;
static const char *readdir_name = "old";
static const char *readlink_target = "target-before";
static unsigned int readdir_calls;
static unsigned int readlink_calls;
static char thread_failed;

static int fail(const char *msg)
{
	fprintf(stderr, "%s\n", msg);
	return 1;
}

static void gate_wait_if_blocked(struct gate *gate)
{
	pthread_mutex_lock(&gate->lock);
	if (gate->block) {
		gate->started = 1;
		pthread_cond_signal(&gate->started_cond);
		while (!gate->release)
			pthread_cond_wait(&gate->release_cond, &gate->lock);
		gate->release = 0;
		gate->started = 0;
	}
	pthread_mutex_unlock(&gate->lock);
}

static void gate_wait_started(struct gate *gate)
{
	pthread_mutex_lock(&gate->lock);
	while (!gate->started)
		pthread_cond_wait(&gate->started_cond, &gate->lock);
	pthread_mutex_unlock(&gate->lock);
}

static void gate_release(struct gate *gate)
{
	pthread_mutex_lock(&gate->lock);
	gate->release = 1;
	pthread_cond_signal(&gate->release_cond);
	pthread_mutex_unlock(&gate->lock);
}

static int collect_filler(void *buf, const char *name,
			  const struct stat *stbuf, off_t off,
			  enum fuse_fill_dir_flags flags)
{
	struct fill_result *result = buf;

	(void) stbuf;
	(void) off;
	(void) flags;

	result->count++;
	snprintf(result->last_name, sizeof(result->last_name), "%s", name);
	return 0;
}

static int fake_opendir(const char *path, struct fuse_file_info *fi)
{
	(void) path;
	fi->fh = 1;
	return 0;
}

static int fake_releasedir(const char *path, struct fuse_file_info *fi)
{
	(void) path;
	(void) fi;
	return 0;
}

static int fake_readdir(const char *path, void *buf, fuse_fill_dir_t filler,
			off_t offset, struct fuse_file_info *fi,
			enum fuse_readdir_flags flags)
{
	struct stat stbuf;

	(void) path;
	(void) offset;
	(void) fi;
	(void) flags;

	readdir_calls++;
	gate_wait_if_blocked(&readdir_gate);

	memset(&stbuf, 0, sizeof(stbuf));
	stbuf.st_mode = S_IFREG | 0644;
	return filler(buf, readdir_name, &stbuf, 0, 0);
}

static int fake_readlink(const char *path, char *buf, size_t size)
{
	(void) path;

	readlink_calls++;
	gate_wait_if_blocked(&readlink_gate);

	snprintf(buf, size, "%s", readlink_target);
	return 0;
}

static void *readdir_thread(void *data)
{
	struct fuse_file_info fi;
	struct fill_result *result = data;

	memset(&fi, 0, sizeof(fi));
	if (cache_oper->opendir("/dir", &fi) != 0)
		return &thread_failed;
	if (cache_oper->readdir("/dir", result, collect_filler, 0, &fi, 0) != 0)
		return &thread_failed;
	if (cache_oper->releasedir("/dir", &fi) != 0)
		return &thread_failed;
	return NULL;
}

static void *readlink_thread(void *data)
{
	char *buf = data;

	if (cache_oper->readlink("/link", buf, 64) != 0)
		return &thread_failed;
	return NULL;
}

static int test_readdir_publication_suppressed(void)
{
	pthread_t thread;
	void *thread_res;
	struct fuse_file_info fi;
	struct fill_result first = { 0 };
	struct fill_result second = { 0 };
	unsigned int calls_after_first;

	readdir_gate.block = 1;
	readdir_name = "old";
	if (pthread_create(&thread, NULL, readdir_thread, &first) != 0)
		return fail("pthread_create readdir failed");

	gate_wait_started(&readdir_gate);
	cache_invalidate_connection();
	gate_release(&readdir_gate);
	if (pthread_join(thread, &thread_res) != 0 || thread_res != NULL)
		return fail("blocked readdir failed");

	if (first.count != 1 || strcmp(first.last_name, "old") != 0)
		return fail("blocked readdir did not return old result");

	calls_after_first = readdir_calls;
	readdir_gate.block = 0;
	readdir_name = "fresh";
	memset(&fi, 0, sizeof(fi));
	if (cache_oper->opendir("/dir", &fi) != 0)
		return fail("second opendir failed");
	if (cache_oper->readdir("/dir", &second, collect_filler, 0, &fi, 0) != 0)
		return fail("second readdir failed");
	if (cache_oper->releasedir("/dir", &fi) != 0)
		return fail("second releasedir failed");

	if (readdir_calls != calls_after_first + 1)
		return fail("stale readdir result was published to cache");
	if (second.count != 1 || strcmp(second.last_name, "fresh") != 0)
		return fail("second readdir did not fetch fresh result");

	return 0;
}

static int test_readlink_publication_suppressed(void)
{
	pthread_t thread;
	void *thread_res;
	char first[64] = { 0 };
	char second[64] = { 0 };
	unsigned int calls_after_first;

	readlink_gate.block = 1;
	readlink_target = "target-before";
	if (pthread_create(&thread, NULL, readlink_thread, first) != 0)
		return fail("pthread_create readlink failed");

	gate_wait_started(&readlink_gate);
	cache_invalidate_connection();
	gate_release(&readlink_gate);
	if (pthread_join(thread, &thread_res) != 0 || thread_res != NULL)
		return fail("blocked readlink failed");

	if (strcmp(first, "target-before") != 0)
		return fail("blocked readlink did not return old target");

	calls_after_first = readlink_calls;
	readlink_gate.block = 0;
	readlink_target = "target-after";
	if (cache_oper->readlink("/link", second, sizeof(second)) != 0)
		return fail("second readlink failed");

	if (readlink_calls != calls_after_first + 1)
		return fail("stale readlink result was published to cache");
	if (strcmp(second, "target-after") != 0)
		return fail("second readlink did not fetch fresh target");

	return 0;
}

int main(void)
{
	struct fuse_args args = FUSE_ARGS_INIT(0, NULL);
	struct fuse_operations fake_oper = {
		.opendir = fake_opendir,
		.readdir = fake_readdir,
		.releasedir = fake_releasedir,
		.readlink = fake_readlink,
	};

	if (cache_parse_options(&args) != 0)
		return fail("cache_parse_options failed");

	cache_oper = cache_wrap(&fake_oper);
	if (cache_oper == NULL)
		return fail("cache_wrap failed");

	if (test_readdir_publication_suppressed() != 0)
		return 1;
	if (test_readlink_publication_suppressed() != 0)
		return 1;

	return 0;
}
