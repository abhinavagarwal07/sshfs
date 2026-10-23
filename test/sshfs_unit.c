/*
 * Tests for the connection handling in sshfs.c.  They need neither a mount
 * nor ssh: sshfs.c is #included so that its static functions and global state
 * can be driven directly, and a "connection" is a socketpair with a tiny SFTP
 * responder thread on the far end.  Dropping a connection is shutdown() on the
 * responder's end.
 *
 * Everything here is prefixed t_ because it shares a translation unit with
 * sshfs.c.  Failures are reported on stderr and make the test exit non-zero.
 */

/* sshfs.c sets _GNU_SOURCE, so it has to come before any other header */
#ifdef __APPLE__
int sshfs_main(int argc, char *argv[], char *envp[], char **exec_path);
#else
int sshfs_main(int argc, char *argv[]);
#endif
#define main sshfs_main
#include "../sshfs.c"
#undef main

#include <sys/resource.h>

#define T_MAX_CONNS 2
#define T_MAX_SEEN 32

/* Something that goes wrong in a test should end it, not hang it. This is
   less than the 30 seconds meson allows a test. */
#define T_TIMEOUT_SECS 25

struct t_responder {
	int fd;
	pthread_t thread;
	pthread_mutex_t lock;
	pthread_cond_t cond;
	int hold;		/* delay the answers until released */
	unsigned nseen;		/* request types received, apart from INIT */
	uint8_t seen[T_MAX_SEEN];
};

static int t_fail(const char *msg)
{
	fprintf(stderr, "FAIL: %s\n", msg);
	return 1;
}

static uint32_t t_get_be32(const uint8_t *p)
{
	return ((uint32_t) p[0] << 24) | ((uint32_t) p[1] << 16) |
	       ((uint32_t) p[2] << 8) | (uint32_t) p[3];
}

static void t_put_be32(uint8_t *p, uint32_t v)
{
	p[0] = v >> 24;
	p[1] = v >> 16;
	p[2] = v >> 8;
	p[3] = v;
}

static int t_read_full(int fd, uint8_t *p, size_t len)
{
	while (len) {
		ssize_t n = read(fd, p, len);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			return -1;
		p += n;
		len -= n;
	}
	return 0;
}

static int t_write_full(int fd, const uint8_t *p, size_t len)
{
	while (len) {
		ssize_t n = write(fd, p, len);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			return -1;
		p += n;
		len -= n;
	}
	return 0;
}

static void t_reply_version(int fd)
{
	uint8_t msg[9];

	t_put_be32(msg, 5);
	msg[4] = SSH_FXP_VERSION;
	t_put_be32(msg + 5, PROTO_VERSION);
	(void) t_write_full(fd, msg, sizeof(msg));
}

/* STATUS: no such file, empty message and language tag */
static void t_reply_status(int fd, uint32_t id)
{
	uint8_t msg[21];

	t_put_be32(msg, 17);
	msg[4] = SSH_FXP_STATUS;
	t_put_be32(msg + 5, id);
	t_put_be32(msg + 9, SSH_FX_NO_SUCH_FILE);
	t_put_be32(msg + 13, 0);
	t_put_be32(msg + 17, 0);
	(void) t_write_full(fd, msg, sizeof(msg));
}

static void *t_responder_main(void *data)
{
	struct t_responder *r = data;
	uint8_t hdr[4];
	uint8_t body[4096];

	for (;;) {
		uint32_t len;
		uint32_t id;

		if (t_read_full(r->fd, hdr, sizeof(hdr)) == -1)
			break;
		len = t_get_be32(hdr);
		if (len < 1 || len > sizeof(body))
			break;
		if (t_read_full(r->fd, body, len) == -1)
			break;

		if (body[0] == SSH_FXP_INIT) {
			t_reply_version(r->fd);
			continue;
		}
		id = len >= 5 ? t_get_be32(body + 1) : 0;

		pthread_mutex_lock(&r->lock);
		if (r->nseen < T_MAX_SEEN)
			r->seen[r->nseen++] = body[0];
		while (r->hold)
			pthread_cond_wait(&r->cond, &r->lock);
		pthread_mutex_unlock(&r->lock);

		t_reply_status(r->fd, id);
	}
	return NULL;
}

static void t_responder_init(struct t_responder *r)
{
	memset(r, 0, sizeof(*r));
	r->fd = -1;
	pthread_mutex_init(&r->lock, NULL);
	pthread_cond_init(&r->cond, NULL);
}

static void t_responder_hold(struct t_responder *r, int hold)
{
	pthread_mutex_lock(&r->lock);
	r->hold = hold;
	pthread_cond_broadcast(&r->cond);
	pthread_mutex_unlock(&r->lock);
}

static unsigned t_responder_seen(struct t_responder *r, uint8_t type)
{
	unsigned i;
	unsigned n = 0;

	pthread_mutex_lock(&r->lock);
	for (i = 0; i < r->nseen; i++)
		if (r->seen[i] == type)
			n++;
	pthread_mutex_unlock(&r->lock);
	return n;
}

/* Connect conn to a fresh responder, the way a (re)connect would */
static int t_session_start(struct conn *conn, struct t_responder *r)
{
	int sv[2];
	int err;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == -1)
		return -1;
	r->fd = sv[1];
	if (pthread_create(&r->thread, NULL, t_responder_main, r) != 0) {
		close(sv[0]);
		close(sv[1]);
		return -1;
	}

	pthread_mutex_lock(&sshfs.lock);
	conn->rfd = sv[0];
	conn->wfd = sv[0];
	err = start_processing_thread(conn);
	pthread_mutex_unlock(&sshfs.lock);
	return err ? -1 : 0;
}

static int t_wait_until(int (*cond)(void *), void *arg)
{
	int i;

	for (i = 0; i < T_TIMEOUT_SECS * 1000; i++) {
		if (cond(arg))
			return 0;
		usleep(1000);
	}
	return -1;
}

/* The far end goes away; wait until sshfs has cleaned up after it */
static int t_session_wait_dead(void *arg)
{
	struct conn *conn = arg;
	int dead;

	pthread_mutex_lock(&sshfs.lock);
	dead = !conn->processing_thread_started;
	pthread_mutex_unlock(&sshfs.lock);
	return dead;
}

static int t_session_drop(struct conn *conn, struct t_responder *r)
{
	shutdown(r->fd, SHUT_RDWR);
	t_responder_hold(r, 0);
	pthread_join(r->thread, NULL);
	close(r->fd);
	r->fd = -1;
	return t_wait_until(t_session_wait_dead, conn);
}

static int t_reqtab_size_is_one(void *arg)
{
	int ok;

	(void) arg;
	pthread_mutex_lock(&sshfs.lock);
	ok = g_hash_table_size(sshfs.reqtab) == 1;
	pthread_mutex_unlock(&sshfs.lock);
	return ok;
}

static unsigned t_outstanding_len(void)
{
	unsigned len;

	pthread_mutex_lock(&sshfs.lock);
	len = sshfs.outstanding_len;
	pthread_mutex_unlock(&sshfs.lock);
	return len;
}

static int t_outstanding_is(void *arg)
{
	unsigned *want = arg;

	return t_outstanding_len() == *want;
}

static int t_idle(void)
{
	int idle;

	pthread_mutex_lock(&sshfs.lock);
	idle = sshfs.outstanding_len == 0 &&
	       g_hash_table_size(sshfs.reqtab) == 0;
	pthread_mutex_unlock(&sshfs.lock);
	return idle;
}

/* A request that expects an answer, run from its own thread */
struct t_stat_job {
	struct conn *conn;
	int res;
};

static void *t_stat_thread(void *data)
{
	struct t_stat_job *job = data;
	struct buffer buf;

	buf_init(&buf, 0);
	buf_add_string(&buf, "/some/path");
	job->res = sftp_request(job->conn, SSH_FXP_STAT, &buf, SSH_FXP_ATTRS,
				NULL);
	buf_free(&buf);
	return NULL;
}

static int t_stat(struct conn *conn)
{
	struct t_stat_job job = { .conn = conn, .res = 0 };

	t_stat_thread(&job);
	return job.res;
}

/* A request bound to the session it was made for, with a chosen size */
struct t_bound_job {
	struct conn *conn;
	int connver;
	size_t pathlen;
	uint8_t type;
	uint8_t expect;
	int res;
};

static void *t_bound_thread(void *data)
{
	struct t_bound_job *job = data;
	struct buffer buf;
	char *path = g_malloc0(job->pathlen + 1);

	memset(path, 'x', job->pathlen);
	buf_init(&buf, 0);
	buf_add_string(&buf, path);
	job->res = sftp_request_connver(job->conn, job->connver, job->type,
					&buf, job->expect, NULL);
	buf_free(&buf);
	g_free(path);
	return NULL;
}

static int t_connver(struct conn *conn)
{
	int connver;

	pthread_mutex_lock(&sshfs.lock);
	connver = conn->connver;
	pthread_mutex_unlock(&sshfs.lock);
	return connver;
}

/*
 * The accounting of outstanding bytes is shared by all connections. When one
 * connection goes away only its own requests may be given back.
 */
static int t_test_teardown_keeps_other_connections(void)
{
	struct t_responder ra;
	struct t_responder rb;
	struct t_stat_job job = { .conn = &sshfs.conns[0], .res = 0 };
	pthread_t thread;

	t_responder_init(&ra);
	t_responder_init(&rb);
	if (t_session_start(&sshfs.conns[0], &ra) == -1 ||
	    t_session_start(&sshfs.conns[1], &rb) == -1)
		return t_fail("could not start the sessions");

	t_responder_hold(&ra, 1);
	if (pthread_create(&thread, NULL, t_stat_thread, &job) != 0)
		return t_fail("pthread_create failed");
	if (t_wait_until(t_reqtab_size_is_one, NULL) == -1)
		return t_fail("request was not sent");
	if (t_outstanding_len() == 0)
		return t_fail("request was not counted");

	/* The other connection drops while the request is still pending */
	if (t_session_drop(&sshfs.conns[1], &rb) == -1)
		return t_fail("connection 1 was not torn down");
	if (t_outstanding_len() == 0)
		return t_fail("tearing down one connection lost the "
			      "accounting of another one");

	t_responder_hold(&ra, 0);
	pthread_join(thread, NULL);
	if (job.res != -ENOENT)
		return t_fail("request did not get its answer");
	if (t_outstanding_len() != 0)
		return t_fail("outstanding bytes left over after the answer");

	if (t_session_drop(&sshfs.conns[0], &ra) == -1)
		return t_fail("connection 0 was not torn down");
	if (!t_idle())
		return t_fail("requests left behind");
	return 0;
}

/* A request that cannot be sent has to give back what it counted */
static int t_test_failed_send_is_refunded(void)
{
	struct t_responder ra;
	struct conn *conn = &sshfs.conns[0];
	struct t_stat_job job = { .conn = conn, .res = 0 };
	pthread_t thread;

	t_responder_init(&ra);
	if (t_session_start(conn, &ra) == -1)
		return t_fail("could not start the session");

	/* Hold up the writer after the request has been counted and queued */
	pthread_mutex_lock(&conn->lock_write);
	if (pthread_create(&thread, NULL, t_stat_thread, &job) != 0) {
		pthread_mutex_unlock(&conn->lock_write);
		return t_fail("pthread_create failed");
	}
	if (t_wait_until(t_reqtab_size_is_one, NULL) == -1) {
		pthread_mutex_unlock(&conn->lock_write);
		return t_fail("request was not queued");
	}

	/* Writes fail from now on, but the reader keeps waiting */
	shutdown(conn->wfd, SHUT_WR);
	pthread_mutex_unlock(&conn->lock_write);
	pthread_join(thread, NULL);

	if (job.res != -EIO)
		return t_fail("failed send was not reported");
	if (!t_idle())
		return t_fail("failed send left its request counted");

	if (t_session_drop(conn, &ra) == -1)
		return t_fail("connection was not torn down");
	if (t_responder_seen(&ra, SSH_FXP_STAT) != 0)
		return t_fail("the failed request reached the server");
	return 0;
}

/*
 * The connection drops, and another one is set up, after a request has been
 * checked against the old one but before it is written. Its handle means
 * nothing to the new session, so it must not be sent there.
 */
static int t_test_stale_request_not_sent_to_new_session(void)
{
	struct conn *conn = &sshfs.conns[0];
	struct t_responder ra;
	struct t_responder rb;
	struct t_bound_job job = { .conn = conn, .pathlen = 2,
				   .type = SSH_FXP_CLOSE, .expect = 0 };
	pthread_t thread;

	t_responder_init(&ra);
	t_responder_init(&rb);
	if (t_session_start(conn, &ra) == -1)
		return t_fail("could not start the first session");
	job.connver = t_connver(conn);

	/* Hold up the writer after the request has been checked and queued */
	pthread_mutex_lock(&conn->lock_write);
	if (pthread_create(&thread, NULL, t_bound_thread, &job) != 0) {
		pthread_mutex_unlock(&conn->lock_write);
		return t_fail("pthread_create failed");
	}
	if (t_wait_until(t_reqtab_size_is_one, NULL) == -1) {
		pthread_mutex_unlock(&conn->lock_write);
		return t_fail("request was not queued");
	}

	/* Drop the connection and bring up a new one; fds get reused */
	if (t_session_drop(conn, &ra) == -1) {
		pthread_mutex_unlock(&conn->lock_write);
		return t_fail("first session was not torn down");
	}
	if (t_session_start(conn, &rb) == -1) {
		pthread_mutex_unlock(&conn->lock_write);
		return t_fail("could not start the second session");
	}
	pthread_mutex_unlock(&conn->lock_write);
	pthread_join(thread, NULL);

	/* The answer to this arrives after everything written before it */
	if (t_stat(conn) != -ENOENT)
		return t_fail("new session does not answer");
	if (t_responder_seen(&rb, SSH_FXP_CLOSE) != 0)
		return t_fail("a request for the old session reached the "
			      "new one");

	if (t_session_drop(conn, &rb) == -1)
		return t_fail("second session was not torn down");
	if (!t_idle())
		return t_fail("requests left behind");
	return 0;
}

/*
 * Requests that wait for room in the send window are not known to the
 * teardown. When the connection they were made for is gone they have to give
 * up and give back what they counted, also while others are still over the
 * limit.
 */
static int t_test_waiting_sender_gives_up_after_reconnect(void)
{
	struct conn *conn = &sshfs.conns[0];
	struct t_responder ra;
	struct t_bound_job jobs[3];
	pthread_t threads[3];
	unsigned counted;
	int connver;
	int i;

	/* The three requests are 80, 60 and 60 bytes long on the wire */
	size_t pathlen[3] = { 67, 47, 47 };

	t_responder_init(&ra);
	sshfs.max_outstanding_len = 100;
	if (t_session_start(conn, &ra) == -1)
		return t_fail("could not start the session");
	connver = t_connver(conn);

	t_responder_hold(&ra, 1);
	for (i = 0; i < 3; i++) {
		jobs[i].conn = conn;
		jobs[i].connver = connver;
		jobs[i].pathlen = pathlen[i];
		jobs[i].type = SSH_FXP_STAT;
		jobs[i].expect = SSH_FXP_ATTRS;
		jobs[i].res = 0;
		if (pthread_create(&threads[i], NULL, t_bound_thread,
				   &jobs[i]) != 0)
			return t_fail("pthread_create failed");

		/* One after the other, so that the first one gets the room */
		counted = i == 0 ? 80 : (i == 1 ? 140 : 200);
		if (t_wait_until(t_outstanding_is, &counted) == -1)
			return t_fail("request was not counted");
	}

	if (t_session_drop(conn, &ra) == -1)
		return t_fail("session was not torn down");
	for (i = 0; i < 3; i++) {
		pthread_join(threads[i], NULL);
		if (jobs[i].res != -EIO)
			return t_fail("request for a lost connection did not "
				      "fail");
	}

	sshfs.max_outstanding_len = ~0U;
	if (!t_idle())
		return t_fail("requests left behind or still counted");
	return 0;
}

/* Wait until a writer has filled the socket, so that it is blocked in write */
static int t_wait_socket_full(int fd)
{
	struct pollfd pfd = { .fd = fd, .events = POLLOUT };
	int i;

	for (i = 0; i < T_TIMEOUT_SECS * 1000; i++) {
		int res = poll(&pfd, 1, 0);

		if (res == 0)
			return 0;
		if (res == -1 && errno != EINTR)
			return -1;
		if (res > 0 && (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)))
			return -1;
		usleep(1000);
	}
	return -1;
}

/*
 * A writer that is blocked on a full socket must not stay blocked once the
 * connection is retired, or the next reconnect would wait for it forever.
 * The far end stays open but does not read, and then sends rubbish so that
 * sshfs gives up on the connection.
 */
static int t_test_blocked_writer_is_released(void)
{
	struct conn *conn = &sshfs.conns[0];
	struct t_responder ra;
	struct t_bound_job job = { .conn = conn, .pathlen = 8 << 20,
				   .type = SSH_FXP_STAT,
				   .expect = SSH_FXP_ATTRS };
	static const uint8_t rubbish[8] = { 0xff, 0xff, 0xff, 0xff,
					    0xff, 0xff, 0xff, 0xff };
	int sndbuf = 4096;
	pthread_t thread;

	t_responder_init(&ra);
	if (t_session_start(conn, &ra) == -1)
		return t_fail("could not start the session");
	job.connver = t_connver(conn);

	/* A small send buffer, and a request that cannot fit in it */
	if (setsockopt(conn->wfd, SOL_SOCKET, SO_SNDBUF, &sndbuf,
		       sizeof(sndbuf)) == -1)
		return t_fail("setsockopt failed");
	if (pthread_create(&thread, NULL, t_bound_thread, &job) != 0)
		return t_fail("pthread_create failed");
	if (t_wait_socket_full(conn->wfd) == -1)
		return t_fail("writer did not fill the socket");

	if (t_write_full(ra.fd, rubbish, sizeof(rubbish)) == -1)
		return t_fail("could not write to the connection");
	if (t_wait_until(t_session_wait_dead, conn) == -1)
		return t_fail("connection was not retired");

	pthread_join(thread, NULL);
	if (job.res != -EIO)
		return t_fail("blocked request did not fail");

	if (t_session_drop(conn, &ra) == -1)
		return t_fail("session was not torn down");
	if (!t_idle())
		return t_fail("requests left behind");
	return 0;
}

/* Running out of descriptors for the private reference must not leak counts */
static int t_test_dup_failure_is_refunded(void)
{
	struct conn *conn = &sshfs.conns[0];
	struct t_responder ra;
	struct rlimit lim;
	struct rlimit saved;
	struct t_bound_job job = { .conn = conn, .pathlen = 2,
				   .type = SSH_FXP_STAT,
				   .expect = SSH_FXP_ATTRS };
	int spare;

	t_responder_init(&ra);
	if (t_session_start(conn, &ra) == -1)
		return t_fail("could not start the session");
	job.connver = t_connver(conn);

	/* Allow no descriptor above the lowest free one (as dup_cloexec picks) */
	spare = fcntl(STDERR_FILENO, F_DUPFD, 3);
	if (spare == -1 || getrlimit(RLIMIT_NOFILE, &saved) == -1)
		return t_fail("could not prepare the descriptor limit");
	close(spare);
	lim = saved;
	lim.rlim_cur = spare;
	if (setrlimit(RLIMIT_NOFILE, &lim) == -1)
		return t_fail("setrlimit failed");

	t_bound_thread(&job);

	if (setrlimit(RLIMIT_NOFILE, &saved) == -1)
		return t_fail("could not restore the descriptor limit");
	if (job.res != -EIO)
		return t_fail("request did not fail without a descriptor");
	if (!t_idle())
		return t_fail("failed request left its count behind");
	if (t_responder_seen(&ra, SSH_FXP_STAT) != 0)
		return t_fail("the failed request reached the server");

	if (t_session_drop(conn, &ra) == -1)
		return t_fail("session was not torn down");
	return 0;
}

int main(void)
{
	int i;

	alarm(T_TIMEOUT_SECS);

	sshfs.max_conns = T_MAX_CONNS;
	sshfs.conns = g_new0(struct conn, T_MAX_CONNS);
	for (i = 0; i < T_MAX_CONNS; i++) {
		sshfs.conns[i].rfd = -1;
		sshfs.conns[i].wfd = -1;
	}
	/* main() sets these; close_conn() would close fd 0 for a zero */
	sshfs.ptyfd = -1;
	sshfs.ptypassivefd = -1;
	sshfs.reconnect = 1;
	sshfs.max_outstanding_len = ~0U;
	if (processing_init() == -1)
		return t_fail("processing_init failed");

	if (t_test_teardown_keeps_other_connections() != 0)
		return 1;
	if (t_test_failed_send_is_refunded() != 0)
		return 1;
	if (t_test_stale_request_not_sent_to_new_session() != 0)
		return 1;
	if (t_test_waiting_sender_gives_up_after_reconnect() != 0)
		return 1;
	if (t_test_dup_failure_is_refunded() != 0)
		return 1;
	if (t_test_blocked_writer_is_released() != 0)
		return 1;

	return 0;
}
