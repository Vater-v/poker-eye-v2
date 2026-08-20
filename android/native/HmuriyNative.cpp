#include <jni.h>

#include <android/log.h>
#include <android/trace.h>

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cinttypes>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <deque>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#define HLOGI(...) __android_log_print(ANDROID_LOG_INFO,  "Hmuriy", __VA_ARGS__)
#define HLOGW(...) __android_log_print(ANDROID_LOG_WARN,  "Hmuriy", __VA_ARGS__)
#define HLOGE(...) __android_log_print(ANDROID_LOG_ERROR, "Hmuriy", __VA_ARGS__)

#ifndef POKEREYE_BUILD_ID
#define POKEREYE_BUILD_ID "dev-unversioned"
#endif

namespace {

constexpr uint32_t kMagic = 0x484D4E31u;  // "HMN1"
constexpr uint8_t kProtocol = 1;
constexpr uint8_t kMsgWsFrame = 0x01;
constexpr uint8_t kMsgHeartbeat = 0x02;
constexpr uint8_t kMsgActionResult = 0x03;
constexpr uint8_t kMsgUiDump = 0x04;
constexpr uint8_t kMsgCommand = 0x81;
constexpr uint8_t kMsgHeartbeatAck = 0x82;

constexpr size_t kQueueMax = 2048;
constexpr size_t kMaxWireFrame = 16u * 1024u * 1024u;
constexpr uint64_t kHeartbeatNs = 1ull * 1000ull * 1000ull * 1000ull;
constexpr uint64_t kTrainerSilenceNs = 15ull * 1000ull * 1000ull * 1000ull;
constexpr uint64_t kReconnectNs = 1000ull * 1000ull * 1000ull;
constexpr uint64_t kStatsNs = 60ull * 1000ull * 1000ull * 1000ull;
constexpr uint64_t kDeviceActionGapNs = 450ull * 1000ull * 1000ull;
constexpr uint64_t kQueuedFrameMaxAgeNs = 30ull * 1000ull * 1000ull * 1000ull;

JavaVM* g_vm = nullptr;
jclass g_bridge_class = nullptr;
jmethodID g_send_synthetic = nullptr;
jmethodID g_hud_method = nullptr;
std::mutex g_hud_mu;
std::string g_pending_hud;

jclass g_system_class = nullptr;
jmethodID g_identity_hash = nullptr;

jclass g_bs_class = nullptr;
jmethodID g_bs_size = nullptr;
jmethodID g_bs_get_byte = nullptr;
jmethodID g_bs_to_byte_array = nullptr;

std::atomic<bool> g_initialized{false};
std::atomic<bool> g_running{false};
std::atomic<bool> g_worker_started{false};
std::atomic<bool> g_welcome_confirmed{false};

std::string g_device_id;
std::string g_transport_id;
std::string g_proof;
std::string g_trainer_host;
std::string g_trainer_fallback;
std::string g_device_label;
int g_port = 19037;
constexpr int kConnectMs = 3000;
constexpr int kHandshakeMs = 3000;
constexpr int kFallbackConnectMs = 8000;

struct FrameRef {
    uint64_t seq = 0;
    uint64_t queued_ns = 0;
    uint32_t ws_id = 0;
    uint8_t direction = 0;
    jobject byte_string = nullptr; // global ref, owned by queue/worker
};

std::mutex g_queue_mu;
std::deque<FrameRef> g_queue;
int g_wake_fd = -1;
std::atomic<uint64_t> g_seq{0};

std::mutex g_ws_mu;
std::unordered_map<uint32_t, jobject> g_ws_refs; // global refs

struct ScheduledAction {
    uint64_t due_ns = 0;
    uint32_t ws_id = 0;
    std::string token;
    std::vector<uint8_t> payload;
};

std::mutex g_action_mu;
std::vector<ScheduledAction> g_actions;
uint64_t g_last_action_send_ns = 0;

std::thread g_worker;

std::atomic<uint64_t> m_tap_calls{0};
std::atomic<bool> m_first_coin_tap_logged{false};
std::atomic<uint64_t> m_tap_ns{0};
std::atomic<uint64_t> m_tap_max_ns{0};
std::array<std::atomic<uint64_t>, 24> m_tap_hist{};

std::atomic<uint64_t> m_queue_drops{0};
std::atomic<uint64_t> m_queue_contention{0};
std::atomic<uint64_t> m_queue_highwater{0};
std::atomic<uint64_t> m_non_coin_binary{0};

std::atomic<uint64_t> m_worker_frames{0};
std::atomic<uint64_t> m_worker_bytes{0};
std::atomic<uint64_t> m_copy_ns{0};
std::atomic<uint64_t> m_tx_ns{0};
std::atomic<uint64_t> m_reconnects{0};
std::atomic<uint64_t> m_commands{0};

std::mutex g_dump_mu;
std::string g_pending_dump;
std::atomic<uint64_t> m_actions_sent{0};
std::atomic<uint64_t> m_action_fail{0};

uint64_t now_ns() {
    timespec ts{};
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ull
        + static_cast<uint64_t>(ts.tv_nsec);
}

void atomic_max(std::atomic<uint64_t>& target, uint64_t value) {
    uint64_t old = target.load(std::memory_order_relaxed);
    while (old < value
            && !target.compare_exchange_weak(
                old, value, std::memory_order_relaxed, std::memory_order_relaxed)) {
    }
}

unsigned hist_bucket_us(uint64_t ns) {
    uint64_t us = std::max<uint64_t>(1, ns / 1000ull);
    unsigned bucket = 0;
    while (us > 1 && bucket + 1 < m_tap_hist.size()) {
        us >>= 1;
        ++bucket;
    }
    return bucket;
}

uint64_t hist_percentile_us(
        const std::array<uint64_t, 24>& bins, uint64_t total, double pct) {
    if (!total) return 0;
    const uint64_t want = static_cast<uint64_t>(total * pct + 0.999);
    uint64_t seen = 0;
    for (size_t i = 0; i < bins.size(); ++i) {
        seen += bins[i];
        if (seen >= want) return 1ull << i;
    }
    return 1ull << (bins.size() - 1);
}

void put_u16(std::vector<uint8_t>& out, uint16_t value) {
    out.push_back(static_cast<uint8_t>((value >> 8) & 0xff));
    out.push_back(static_cast<uint8_t>(value & 0xff));
}

void put_u32(std::vector<uint8_t>& out, uint32_t value) {
    out.push_back(static_cast<uint8_t>((value >> 24) & 0xff));
    out.push_back(static_cast<uint8_t>((value >> 16) & 0xff));
    out.push_back(static_cast<uint8_t>((value >> 8) & 0xff));
    out.push_back(static_cast<uint8_t>(value & 0xff));
}

void put_u64(std::vector<uint8_t>& out, uint64_t value) {
    for (int shift = 56; shift >= 0; shift -= 8) {
        out.push_back(static_cast<uint8_t>((value >> shift) & 0xff));
    }
}

bool get_u16(const uint8_t*& p, const uint8_t* end, uint16_t& out) {
    if (end - p < 2) return false;
    out = (static_cast<uint16_t>(p[0]) << 8)
        | static_cast<uint16_t>(p[1]);
    p += 2;
    return true;
}

bool get_u32(const uint8_t*& p, const uint8_t* end, uint32_t& out) {
    if (end - p < 4) return false;
    out = (static_cast<uint32_t>(p[0]) << 24)
        | (static_cast<uint32_t>(p[1]) << 16)
        | (static_cast<uint32_t>(p[2]) << 8)
        | static_cast<uint32_t>(p[3]);
    p += 4;
    return true;
}

bool send_all(int fd, const uint8_t* data, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t n = send(fd, data + offset, size - offset, MSG_NOSIGNAL);
        if (n > 0) {
            offset += static_cast<size_t>(n);
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        return false;
    }
    return true;
}

bool recv_all(int fd, uint8_t* data, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t n = recv(fd, data + offset, size - offset, 0);
        if (n > 0) {
            offset += static_cast<size_t>(n);
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        return false;
    }
    return true;
}

bool send_frame(int fd, const std::vector<uint8_t>& body) {
    if (body.empty() || body.size() > kMaxWireFrame) return false;
    uint32_t n = static_cast<uint32_t>(body.size());
    uint8_t hdr[4] = {
        static_cast<uint8_t>((n >> 24) & 0xff),
        static_cast<uint8_t>((n >> 16) & 0xff),
        static_cast<uint8_t>((n >> 8) & 0xff),
        static_cast<uint8_t>(n & 0xff),
    };
    return send_all(fd, hdr, sizeof(hdr))
        && send_all(fd, body.data(), body.size());
}

bool send_frame(int fd, const std::string& body) {
    return send_frame(
        fd, std::vector<uint8_t>(body.begin(), body.end()));
}

bool recv_frame(int fd, std::vector<uint8_t>& out) {
    uint8_t hdr[4];
    if (!recv_all(fd, hdr, sizeof(hdr))) return false;
    uint32_t n = (static_cast<uint32_t>(hdr[0]) << 24)
        | (static_cast<uint32_t>(hdr[1]) << 16)
        | (static_cast<uint32_t>(hdr[2]) << 8)
        | static_cast<uint32_t>(hdr[3]);
    if (!n || n > kMaxWireFrame) return false;
    out.resize(n);
    return recv_all(fd, out.data(), n);
}

void wake_worker() {
    if (g_wake_fd < 0) return;
    uint64_t one = 1;
    ssize_t ignored = write(g_wake_fd, &one, sizeof(one));
    (void)ignored;
}

void drain_wake_fd() {
    if (g_wake_fd < 0) return;
    uint64_t value = 0;
    while (read(g_wake_fd, &value, sizeof(value)) > 0) {
    }
}

bool ensure_byte_string_methods(JNIEnv*, jobject) {
    return g_bs_class && g_bs_size && g_bs_get_byte && g_bs_to_byte_array;
}

void register_ws_nonblocking(JNIEnv* env, uint32_t ws_id, jobject ws) {
    std::unique_lock<std::mutex> lock(g_ws_mu, std::try_to_lock);
    if (!lock.owns_lock()) return;

    auto it = g_ws_refs.find(ws_id);
    if (it != g_ws_refs.end()) {
        if (env->IsSameObject(it->second, ws)) return;
        env->DeleteGlobalRef(it->second);
        g_ws_refs.erase(it);
    }

    jobject global = env->NewGlobalRef(ws);
    if (global) g_ws_refs.emplace(ws_id, global);
}

jobject local_ws_ref(JNIEnv* env, uint32_t ws_id) {
    std::lock_guard<std::mutex> lock(g_ws_mu);
    auto it = g_ws_refs.find(ws_id);
    if (it == g_ws_refs.end()) return nullptr;
    return env->NewLocalRef(it->second);
}

void record_tap(uint64_t elapsed) {
    m_tap_calls.fetch_add(1, std::memory_order_relaxed);
    m_tap_ns.fetch_add(elapsed, std::memory_order_relaxed);
    atomic_max(m_tap_max_ns, elapsed);
    m_tap_hist[hist_bucket_us(elapsed)].fetch_add(1, std::memory_order_relaxed);
}

int connect_tcp(
        const std::string& host,
        int timeout_ms = 2500) {
    // Plain IPv4 TCP only. Android/SocksDroid owns routing. PokerEye does not
    // bind a Network/interface/source address and does not use LAN/ADB bypasses.
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;

    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));

    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) {
        close(fd);
        return -1;
    }
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(g_port));
    if (inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
        close(fd);
        return -1;
    }

    int rc = connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    if (rc < 0 && errno != EINPROGRESS) {
        close(fd);
        return -1;
    }

    pollfd pfd{fd, POLLOUT, 0};
    rc = poll(&pfd, 1, std::max(100, timeout_ms));
    if (rc <= 0 || !(pfd.revents & POLLOUT)) {
        close(fd);
        return -1;
    }

    int error = 0;
    socklen_t error_len = sizeof(error);
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &error_len) != 0 || error != 0) {
        close(fd);
        return -1;
    }

    fcntl(fd, F_SETFL, flags);
    return fd;
}

std::string json_escape(const std::string& value) {
    std::ostringstream out;
    for (unsigned char c : value) {
        switch (c) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (c >= 0x20) out << static_cast<char>(c);
                break;
        }
    }
    return out.str();
}

std::string hello_json() {
    std::ostringstream ss;
    ss << "{\"type\":\"direct_hello\",\"version\":2,\"device_id\":\""
       << json_escape(g_device_id)
       << "\",\"table_id\":\""
       << json_escape(g_transport_id)
       << "\",\"proof\":\""
       << json_escape(g_proof)
       << "\",\"native_mux\":1,\"build_id\":\""
       << POKEREYE_BUILD_ID
       << "\",\"device_label\":\""
       << json_escape(g_device_label)
       << "\"}";
    return ss.str();
}

bool parse_command(const std::vector<uint8_t>& body);

bool handshake_confirmed(
        int fd,
        const std::string& host,
        int timeout_ms) {
    g_welcome_confirmed.store(false, std::memory_order_release);
    if (!send_frame(fd, hello_json())) return false;
    HLOGI(
        "[+] trainer hello sent host=%s:%d build=%s; waiting welcome up to %dms",
        host.c_str(), g_port, POKEREYE_BUILD_ID, timeout_ms);

    timeval tv{};
    tv.tv_sec = std::max(1, timeout_ms) / 1000;
    tv.tv_usec = (std::max(1, timeout_ms) % 1000) * 1000;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    const uint64_t start = now_ns();
    std::vector<uint8_t> reply;
    if (!recv_frame(fd, reply)) {
        HLOGW(
            "[~] trainer welcome timeout host=%s:%d after=%dms errno=%d",
            host.c_str(), g_port, timeout_ms, errno);
        return false;
    }
    HLOGI(
        "[+] trainer welcome bytes=%zu first=%02x host=%s:%d",
        reply.size(),
        reply.empty() ? 0 : reply.front(),
        host.c_str(),
        g_port);
    if (!parse_command(reply) || !g_welcome_confirmed.load(std::memory_order_acquire)) {
        std::string head(reply.begin(), reply.begin() + static_cast<ptrdiff_t>(std::min<size_t>(reply.size(), 80)));
        HLOGW("[~] trainer welcome not parsed: %s", head.c_str());
        return false;
    }

    tv.tv_sec = 0;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    const uint64_t rtt_ms = (now_ns() - start) / 1000000ull;
    HLOGI(
        "[+] trainer VPS handshake confirmed host=%s:%d rtt=%" PRIu64 "ms ipv4=1",
        host.c_str(), g_port, rtt_ms);
    return true;
}

std::vector<uint8_t> make_heartbeat(uint64_t seq) {
    std::vector<uint8_t> body;
    body.reserve(16);
    put_u32(body, kMagic);
    body.push_back(kMsgHeartbeat);
    body.push_back(kProtocol);
    put_u16(body, 0);
    put_u64(body, seq);
    return body;
}

std::vector<uint8_t> make_ws_frame(
        const FrameRef& frame, const uint8_t* payload, size_t payload_size) {
    std::vector<uint8_t> body;
    body.reserve(28 + payload_size);
    put_u32(body, kMagic);
    body.push_back(kMsgWsFrame);
    body.push_back(kProtocol);
    put_u16(body, 0);
    put_u64(body, frame.seq);
    put_u32(body, frame.ws_id);
    body.push_back(frame.direction);
    body.push_back(0);
    body.push_back(0);
    body.push_back(0);
    put_u32(body, static_cast<uint32_t>(payload_size));
    body.insert(body.end(), payload, payload + payload_size);
    return body;
}

void cancel_action(const std::string& token) {
    if (token.empty()) return;
    std::lock_guard<std::mutex> lock(g_action_mu);
    g_actions.erase(
        std::remove_if(
            g_actions.begin(),
            g_actions.end(),
            [&](const ScheduledAction& action) {
                return action.token == token;
            }),
        g_actions.end());
}

void schedule_action(
        uint32_t ws_id,
        uint32_t delay_ms,
        std::string token,
        std::vector<uint8_t> payload) {
    const uint64_t requested =
        now_ns() + static_cast<uint64_t>(delay_ms) * 1000000ull;

    std::lock_guard<std::mutex> lock(g_action_mu);
    if (!token.empty()) {
        g_actions.erase(
            std::remove_if(
                g_actions.begin(),
                g_actions.end(),
                [&](const ScheduledAction& a) { return a.token == token; }),
            g_actions.end());
    }

    ScheduledAction action;
    action.due_ns = requested;
    action.ws_id = ws_id;
    action.token = std::move(token);
    action.payload = std::move(payload);
    g_actions.emplace_back(std::move(action));
}

bool send_action_result(
        int fd,
        const ScheduledAction& action,
        bool success,
        uint8_t reason_code) {
    std::string token = action.token;
    if (token.size() > 65535u) token.resize(65535u);
    std::vector<uint8_t> body;
    body.reserve(16 + token.size());
    put_u32(body, kMagic);
    body.push_back(kMsgActionResult);
    body.push_back(kProtocol);
    put_u16(body, success ? 0x01u : 0x00u);
    put_u32(body, action.ws_id);
    put_u16(body, static_cast<uint16_t>(token.size()));
    body.push_back(reason_code);
    body.push_back(0);
    body.insert(body.end(), token.begin(), token.end());
    return send_frame(fd, body);
}

bool parse_command(const std::vector<uint8_t>& body) {
    if (body.empty()) return false;

    // Trainer sends one length-prefixed JSON control frame immediately after the
    // authenticated hello.  With the non-blocking startup above it arrives here,
    // possibly after a few queued Coin frames have already been transmitted.
    if (body.front() == static_cast<uint8_t>('{')) {
        std::string text(body.begin(), body.end());
        if (text.find("\"type\":\"welcome\"") != std::string::npos
                || text.find("\"type\": \"welcome\"") != std::string::npos) {
            bool expected = false;
            if (g_welcome_confirmed.compare_exchange_strong(
                    expected, true, std::memory_order_acq_rel,
                    std::memory_order_acquire)) {
                HLOGI(
                    "[+] trainer handshake confirmed device=%s build=%s",
                    g_device_id.c_str(), POKEREYE_BUILD_ID);
            }
            return true;
        }
        if (text.find("\"type\":\"hud\"") != std::string::npos
                || text.find("\"type\": \"hud\"") != std::string::npos) {
            std::lock_guard<std::mutex> lock(g_hud_mu);
            g_pending_hud = text;
            wake_worker();
            return true;
        }
        HLOGW("[~] trainer rejected native hello: %s", text.c_str());
        return false;
    }

    const uint8_t* p = body.data();
    const uint8_t* end = p + body.size();

    uint32_t magic = 0;
    if (!get_u32(p, end, magic) || magic != kMagic) return false;
    if (end - p < 4) return false;

    const uint8_t type = *p++;
    const uint8_t version = *p++;
    uint16_t flags = 0;
    if (!get_u16(p, end, flags)) return false;

    if (version != kProtocol) return false;
    if (type == kMsgHeartbeatAck) return true;
    if (type != kMsgCommand) return false;

    uint32_t ws_id = 0;
    uint32_t delay_ms = 0;
    uint16_t token_len = 0;
    uint16_t cancel_len = 0;
    uint32_t payload_len = 0;
    if (!get_u32(p, end, ws_id)
            || !get_u32(p, end, delay_ms)
            || !get_u16(p, end, token_len)
            || !get_u16(p, end, cancel_len)
            || !get_u32(p, end, payload_len)) {
        return false;
    }

    const size_t needed =
        static_cast<size_t>(token_len)
        + static_cast<size_t>(cancel_len)
        + static_cast<size_t>(payload_len);
    if (static_cast<size_t>(end - p) != needed) return false;

    std::string token(
        reinterpret_cast<const char*>(p),
        reinterpret_cast<const char*>(p + token_len));
    p += token_len;

    std::string cancel(
        reinterpret_cast<const char*>(p),
        reinterpret_cast<const char*>(p + cancel_len));
    p += cancel_len;

    std::vector<uint8_t> payload(p, p + payload_len);

    if ((flags & 0x01u) && !cancel.empty()) cancel_action(cancel);
    if ((flags & 0x02u) && !payload.empty()) {
        schedule_action(ws_id, delay_ms, std::move(token), std::move(payload));
    }

    m_commands.fetch_add(1, std::memory_order_relaxed);
    return true;
}

void dispatch_due_actions(JNIEnv* env, int fd) {
    while (true) {
        ScheduledAction action;
        bool have = false;

        {
            std::lock_guard<std::mutex> lock(g_action_mu);
            if (g_actions.empty()) return;

            const uint64_t now = now_ns();
            size_t best = g_actions.size();
            uint64_t best_due = UINT64_MAX;

            for (size_t i = 0; i < g_actions.size(); ++i) {
                uint64_t effective = std::max(
                    g_actions[i].due_ns,
                    g_last_action_send_ns + kDeviceActionGapNs);
                if (effective < best_due) {
                    best_due = effective;
                    best = i;
                }
            }

            if (best == g_actions.size() || best_due > now) return;
            action = std::move(g_actions[best]);
            g_actions.erase(g_actions.begin() + static_cast<std::ptrdiff_t>(best));
            g_last_action_send_ns = now;
            have = true;
        }

        if (!have) return;

        jobject ws = local_ws_ref(env, action.ws_id);
        if (!ws) {
            m_action_fail.fetch_add(1, std::memory_order_relaxed);
            HLOGW("[~] native action target missing ws=%08x", action.ws_id);
            (void)send_action_result(fd, action, false, 1);
            continue;
        }

        jbyteArray bytes = env->NewByteArray(static_cast<jsize>(action.payload.size()));
        if (!bytes) {
            env->DeleteLocalRef(ws);
            m_action_fail.fetch_add(1, std::memory_order_relaxed);
            if (env->ExceptionCheck()) env->ExceptionClear();
            (void)send_action_result(fd, action, false, 2);
            continue;
        }

        env->SetByteArrayRegion(
            bytes,
            0,
            static_cast<jsize>(action.payload.size()),
            reinterpret_cast<const jbyte*>(action.payload.data()));

        if (ATrace_isEnabled()) ATrace_beginSection("hmuriy.action");
        jboolean ok = env->CallStaticBooleanMethod(
            g_bridge_class, g_send_synthetic, ws, bytes);
        if (ATrace_isEnabled()) ATrace_endSection();

        bool jni_exception = false;
        if (env->ExceptionCheck()) {
            env->ExceptionDescribe();
            env->ExceptionClear();
            ok = JNI_FALSE;
            jni_exception = true;
        }

        env->DeleteLocalRef(bytes);
        env->DeleteLocalRef(ws);

        if (ok == JNI_TRUE) {
            m_actions_sent.fetch_add(1, std::memory_order_relaxed);
            (void)send_action_result(fd, action, true, 0);
        } else {
            m_action_fail.fetch_add(1, std::memory_order_relaxed);
            (void)send_action_result(fd, action, false, jni_exception ? 4 : 3);
        }
    }
}

int next_action_timeout_ms() {
    std::lock_guard<std::mutex> lock(g_action_mu);
    if (g_actions.empty()) return 1000;

    const uint64_t now = now_ns();
    uint64_t earliest = UINT64_MAX;
    for (const auto& action : g_actions) {
        earliest = std::min(
            earliest,
            std::max(
                action.due_ns,
                g_last_action_send_ns + kDeviceActionGapNs));
    }

    if (earliest <= now) return 0;
    uint64_t ms = (earliest - now + 999999ull) / 1000000ull;
    return static_cast<int>(std::min<uint64_t>(ms, 1000));
}

bool dequeue(FrameRef& out) {
    std::lock_guard<std::mutex> lock(g_queue_mu);
    if (g_queue.empty()) return false;
    out = g_queue.front();
    g_queue.pop_front();
    return true;
}

void cleanup_frame(JNIEnv* env, FrameRef& frame) {
    if (frame.byte_string) {
        env->DeleteGlobalRef(frame.byte_string);
        frame.byte_string = nullptr;
    }
}

bool send_one_queued(JNIEnv* env, int fd) {
    FrameRef frame;
    if (!dequeue(frame)) return true;

    const uint64_t age = now_ns() - frame.queued_ns;
    if (age > kQueuedFrameMaxAgeNs) {
        cleanup_frame(env, frame);
        m_queue_drops.fetch_add(1, std::memory_order_relaxed);
        return true;
    }

    const uint64_t copy_start = now_ns();
    if (ATrace_isEnabled()) ATrace_beginSection("hmuriy.copy");
    jobject array_obj = env->CallObjectMethod(
        frame.byte_string, g_bs_to_byte_array);
    if (ATrace_isEnabled()) ATrace_endSection();

    if (env->ExceptionCheck()) {
        env->ExceptionClear();
        cleanup_frame(env, frame);
        return true;
    }

    jbyteArray array = static_cast<jbyteArray>(array_obj);
    if (!array) {
        cleanup_frame(env, frame);
        return true;
    }

    const jsize n = env->GetArrayLength(array);
    if (n <= 0 || static_cast<size_t>(n) > kMaxWireFrame) {
        env->DeleteLocalRef(array);
        cleanup_frame(env, frame);
        return true;
    }

    std::vector<uint8_t> bytes(static_cast<size_t>(n));
    env->GetByteArrayRegion(
        array,
        0,
        n,
        reinterpret_cast<jbyte*>(bytes.data()));
    env->DeleteLocalRef(array);
    cleanup_frame(env, frame);

    const uint64_t copy_elapsed = now_ns() - copy_start;
    m_copy_ns.fetch_add(copy_elapsed, std::memory_order_relaxed);

    std::vector<uint8_t> wire = make_ws_frame(
        frame, bytes.data(), bytes.size());

    const uint64_t tx_start = now_ns();
    if (ATrace_isEnabled()) ATrace_beginSection("hmuriy.tx");
    const bool ok = send_frame(fd, wire);
    if (ATrace_isEnabled()) ATrace_endSection();
    const uint64_t tx_elapsed = now_ns() - tx_start;
    m_tx_ns.fetch_add(tx_elapsed, std::memory_order_relaxed);

    if (!ok) return false;

    m_worker_frames.fetch_add(1, std::memory_order_relaxed);
    m_worker_bytes.fetch_add(bytes.size(), std::memory_order_relaxed);
    return true;
}

void log_stats(uint64_t& last_stats) {
    const uint64_t now = now_ns();
    if (now - last_stats < kStatsNs) return;
    last_stats = now;

    const uint64_t calls = m_tap_calls.exchange(0);
    const uint64_t tap_ns = m_tap_ns.exchange(0);
    const uint64_t tap_max = m_tap_max_ns.exchange(0);
    std::array<uint64_t, 24> hist{};
    for (size_t i = 0; i < hist.size(); ++i) {
        hist[i] = m_tap_hist[i].exchange(0);
    }

    const uint64_t frames = m_worker_frames.exchange(0);
    const uint64_t bytes = m_worker_bytes.exchange(0);
    const uint64_t copy_ns = m_copy_ns.exchange(0);
    const uint64_t tx_ns = m_tx_ns.exchange(0);
    const uint64_t drops = m_queue_drops.exchange(0);
    const uint64_t contention = m_queue_contention.exchange(0);
    const uint64_t reconnects = m_reconnects.exchange(0);
    const uint64_t commands = m_commands.exchange(0);
    const uint64_t actions = m_actions_sent.exchange(0);
    const uint64_t action_fail = m_action_fail.exchange(0);
    const uint64_t non_coin = m_non_coin_binary.exchange(0);

    size_t qsize = 0;
    {
        std::lock_guard<std::mutex> lock(g_queue_mu);
        qsize = g_queue.size();
    }
    const uint64_t high = m_queue_highwater.exchange(qsize);

    const uint64_t avg_tap_us = calls ? (tap_ns / calls) / 1000ull : 0;
    const uint64_t p50 = hist_percentile_us(hist, calls, 0.50);
    const uint64_t p99 = hist_percentile_us(hist, calls, 0.99);
    const uint64_t max_tap_us = tap_max / 1000ull;
    const uint64_t copy_avg_us = frames ? (copy_ns / frames) / 1000ull : 0;
    const uint64_t tx_avg_us = frames ? (tx_ns / frames) / 1000ull : 0;

    if (drops == 0 && contention == 0 && reconnects == 0 && action_fail == 0) {
        return;
    }
    HLOGW(
        "[perf] tap avg=%" PRIu64 "us p50~%" PRIu64 "us p99~%" PRIu64
        "us max=%" PRIu64 "us q=%zu/%zu hi=%" PRIu64
        " drop=%" PRIu64 " contend=%" PRIu64
        " worker=%" PRIu64 "f/5s %" PRIu64 "KiB copy=%" PRIu64
        "us tx=%" PRIu64 "us reconnect=%" PRIu64
        " cmd=%" PRIu64 " action=%" PRIu64 " fail=%" PRIu64
        " noncoin=%" PRIu64 " welcome=%d",
        avg_tap_us, p50, p99, max_tap_us,
        qsize, kQueueMax, high, drops, contention,
        frames, bytes / static_cast<uint64_t>(1024), copy_avg_us, tx_avg_us,
        reconnects, commands, actions, action_fail, non_coin,
        g_welcome_confirmed.load(std::memory_order_acquire) ? 1 : 0);
}

void worker_main();

bool ensure_worker_started() {
    if (g_worker_started.load(std::memory_order_acquire)) return true;
    bool expected = false;
    if (!g_worker_started.compare_exchange_strong(
            expected, true, std::memory_order_acq_rel, std::memory_order_acquire)) {
        return true;
    }
    try {
        g_running.store(true, std::memory_order_release);
        g_worker = std::thread(worker_main);
        g_worker.detach();
        HLOGI(
            "[+] Coin-active process channel started transport=%s",
            g_transport_id.c_str());
        return true;
    } catch (...) {
        g_running.store(false, std::memory_order_release);
        g_worker_started.store(false, std::memory_order_release);
        HLOGE("[!] failed to start native transport worker");
        return false;
    }
}

int try_trainer_uplink(const std::string& host, int connect_ms, int handshake_ms) {
    if (host.empty()) return -1;
    HLOGI(
        "[~] trainer connecting host=%s:%d connect=%dms handshake=%dms",
        host.c_str(), g_port, connect_ms, handshake_ms);
    int fd = connect_tcp(host, connect_ms);
    if (fd < 0) {
        HLOGW(
            "[~] trainer connect fail host=%s:%d errno=%d",
            host.c_str(), g_port, errno);
        return -1;
    }
    if (!handshake_confirmed(fd, host, handshake_ms)) {
        close(fd);
        return -1;
    }
    return fd;
}

void flush_hud(JNIEnv* env) {
    if (!env || !g_bridge_class || !g_hud_method) return;
    std::string json;
    {
        std::lock_guard<std::mutex> lock(g_hud_mu);
        json.swap(g_pending_hud);
    }
    if (json.empty()) return;
    jstring text = env->NewStringUTF(json.c_str());
    if (!text) {
        if (env->ExceptionCheck()) env->ExceptionClear();
        return;
    }
    env->CallStaticVoidMethod(g_bridge_class, g_hud_method, text);
    if (env->ExceptionCheck()) env->ExceptionClear();
    env->DeleteLocalRef(text);
}

void worker_main() {
    JNIEnv* env = nullptr;
    if (g_vm->AttachCurrentThread(&env, nullptr) != JNI_OK || !env) {
        HLOGE("[!] libhmuriy worker could not attach to JVM");
        return;
    }

    uint64_t last_stats = now_ns();
    uint64_t next_heartbeat = now_ns() + kHeartbeatNs;

    while (g_running.load(std::memory_order_acquire)) {
        int fd = -1;
        std::string connected_host = g_trainer_host;

        // Primary VPS first. If hello gets no welcome in 3s, same traffic goes
        // through the NL socat bridge (Sony/USA path that swallows the reply).
        fd = try_trainer_uplink(g_trainer_host, kConnectMs, kHandshakeMs);
        if (fd < 0 && !g_trainer_fallback.empty()
                && g_trainer_fallback != g_trainer_host) {
            HLOGW(
                "[~] trainer primary timeout host=%s:%d; failover via bridge %s:%d",
                g_trainer_host.c_str(), g_port,
                g_trainer_fallback.c_str(), g_port);
            fd = try_trainer_uplink(
                g_trainer_fallback, kFallbackConnectMs, kHandshakeMs);
            if (fd >= 0) connected_host = g_trainer_fallback;
        }
        if (fd < 0) {
            m_reconnects.fetch_add(1, std::memory_order_relaxed);
            log_stats(last_stats);
            std::this_thread::sleep_for(std::chrono::seconds(1));
            continue;
        }

        const char* path =
            (connected_host == g_trainer_fallback
                && connected_host != g_trainer_host)
            ? "nl-bridge" : "vps";
        HLOGI(
            "[+] native uplink open device=%s trainer=%s:%d build=%s welcome=confirmed path=%s ipv4=1",
            g_device_id.c_str(), connected_host.c_str(), g_port, POKEREYE_BUILD_ID, path);
        next_heartbeat = now_ns() + kHeartbeatNs;
        uint64_t last_trainer_rx = now_ns();

        bool connected = true;
        while (connected && g_running.load(std::memory_order_acquire)) {
            dispatch_due_actions(env, fd);

            // Drain a bounded burst so receive/actions still get low latency.
            for (int i = 0; i < 32; ++i) {
                if (!send_one_queued(env, fd)) {
                    connected = false;
                    break;
                }
                std::lock_guard<std::mutex> lock(g_queue_mu);
                if (g_queue.empty()) break;
            }
            if (!connected) break;

            const uint64_t now = now_ns();
            if (now - last_trainer_rx >= kTrainerSilenceNs) {
                HLOGW(
                    "[~] trainer RX watchdog expired host=%s age=%" PRIu64 "ms; reconnecting",
                    connected_host.c_str(), static_cast<uint64_t>((now - last_trainer_rx) / 1000000ull));
                connected = false;
                break;
            }
            std::string dump;
            {
                std::lock_guard<std::mutex> lock(g_dump_mu);
                dump.swap(g_pending_dump);
            }
            if (!dump.empty()) {
                std::vector<uint8_t> body;
                body.reserve(12 + dump.size());
                put_u32(body, kMagic);
                body.push_back(kMsgUiDump);
                body.push_back(kProtocol);
                put_u16(body, 0);
                put_u32(body, static_cast<uint32_t>(dump.size()));
                body.insert(body.end(), dump.begin(), dump.end());
                if (!send_frame(fd, body)) {
                    connected = false;
                    break;
                }
            }
            if (now >= next_heartbeat) {
                if (!send_frame(fd, make_heartbeat(now))) {
                    connected = false;
                    break;
                }
                next_heartbeat = now + kHeartbeatNs;
            }

            int timeout_ms = next_action_timeout_ms();
            uint64_t hb_ms =
                next_heartbeat > now
                ? (next_heartbeat - now + 999999ull) / 1000000ull
                : 0;
            timeout_ms = std::min<int>(
                timeout_ms,
                static_cast<int>(std::min<uint64_t>(hb_ms, 1000)));

            pollfd fds[2] = {
                {fd, POLLIN, 0},
                {g_wake_fd, POLLIN, 0},
            };
            int rc = poll(fds, 2, std::max(0, timeout_ms));
            if (rc < 0) {
                if (errno == EINTR) continue;
                connected = false;
                break;
            }

            if (fds[1].revents & POLLIN) {
                drain_wake_fd();
                flush_hud(env);
            }

            if (fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)) {
                connected = false;
                break;
            }

            if (fds[0].revents & POLLIN) {
                std::vector<uint8_t> command;
                if (!recv_frame(fd, command) || !parse_command(command)) {
                    connected = false;
                    break;
                }
                last_trainer_rx = now_ns();
                flush_hud(env);
            }

            log_stats(last_stats);
        }

        close(fd);
        g_welcome_confirmed.store(false, std::memory_order_release);
        m_reconnects.fetch_add(1, std::memory_order_relaxed);
        HLOGW("[~] native trainer transport dropped; reconnecting in 1s (game unaffected)");
        log_stats(last_stats);
        usleep(static_cast<useconds_t>(kReconnectNs / 1000ull));
    }

    g_vm->DetachCurrentThread();
}

std::string stats_string() {
    size_t qsize = 0;
    {
        std::lock_guard<std::mutex> lock(g_queue_mu);
        qsize = g_queue.size();
    }
    size_t actions = 0;
    {
        std::lock_guard<std::mutex> lock(g_action_mu);
        actions = g_actions.size();
    }
    std::ostringstream ss;
    ss << "device=" << g_device_id
       << " q=" << qsize << "/" << kQueueMax
       << " scheduled=" << actions;
    return ss.str();
}

} // namespace

extern "C" JNIEXPORT jboolean JNICALL
Java_com_hmuriy_HmuriyBridge_nativeInit(
        JNIEnv* env,
        jclass clazz,
        jstring device_id,
        jstring transport_id,
        jstring proof,
        jstring trainer_host,
        jstring trainer_fallback,
        jint port,
        jstring device_label) {
    if (g_initialized.load(std::memory_order_acquire)) {
        return JNI_TRUE;
    }

    auto to_string = [&](jstring value) -> std::string {
        if (!value) return {};
        const char* chars = env->GetStringUTFChars(value, nullptr);
        if (!chars) return {};
        std::string out(chars);
        env->ReleaseStringUTFChars(value, chars);
        return out;
    };

    g_device_id = to_string(device_id);
    g_transport_id = to_string(transport_id);
    g_proof = to_string(proof);
    g_trainer_host = to_string(trainer_host);
    g_trainer_fallback = to_string(trainer_fallback);
    g_port = static_cast<int>(port);
    g_device_label = to_string(device_label);

    g_bridge_class = static_cast<jclass>(env->NewGlobalRef(clazz));
    g_send_synthetic = env->GetStaticMethodID(
        clazz, "sendSynthetic", "(Ljava/lang/Object;[B)Z");
    g_hud_method = env->GetStaticMethodID(
        clazz, "onTrainerHud", "(Ljava/lang/String;)V");
    if (!g_hud_method && env->ExceptionCheck()) env->ExceptionClear();

    if (!g_bridge_class || !g_send_synthetic) {
        if (env->ExceptionCheck()) env->ExceptionClear();
        HLOGE("[!] libhmuriy could not bind Java action dispatcher");
        return JNI_FALSE;
    }

    // Resolve Okio methods once during initialization. The RealWebSocket hot path
    // never performs class/method lookup. nativeInit is invoked from the app class
    // loader, so FindClass resolves the packaged Okio class correctly here.
    jclass bs_local = env->FindClass("okio/ByteString");
    if (!bs_local) {
        if (env->ExceptionCheck()) env->ExceptionClear();
        HLOGE("[!] libhmuriy could not resolve okio.ByteString");
        return JNI_FALSE;
    }
    g_bs_class = static_cast<jclass>(env->NewGlobalRef(bs_local));
    env->DeleteLocalRef(bs_local);
    if (!g_bs_class) {
        HLOGE("[!] libhmuriy could not retain okio.ByteString class");
        return JNI_FALSE;
    }
    g_bs_size = env->GetMethodID(g_bs_class, "size", "()I");
    g_bs_get_byte = env->GetMethodID(g_bs_class, "getByte", "(I)B");
    g_bs_to_byte_array = env->GetMethodID(g_bs_class, "toByteArray", "()[B");
    if (!g_bs_size || !g_bs_get_byte || !g_bs_to_byte_array) {
        if (env->ExceptionCheck()) env->ExceptionClear();
        HLOGE("[!] libhmuriy could not bind okio.ByteString methods");
        return JNI_FALSE;
    }

    g_wake_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (g_wake_fd < 0) {
        HLOGE("[!] libhmuriy eventfd failed errno=%d", errno);
        return JNI_FALSE;
    }

    g_initialized.store(true, std::memory_order_release);
    const bool worker = ensure_worker_started();

    HLOGI(
        "[+] libhmuriy initialized build=%s device=%s transport=%s protocol=HMN1 action_result=1 eager_transport=%d trainer=%s:%d fallback=%s:%d handshake_ms=%d ipv4=1 routing=android-default label=%s",
        POKEREYE_BUILD_ID, g_device_id.c_str(), g_transport_id.c_str(),
        worker ? 1 : 0, g_trainer_host.c_str(), g_port,
        g_trainer_fallback.c_str(), g_port, kHandshakeMs, g_device_label.c_str());
    return JNI_TRUE;
}

extern "C" JNIEXPORT void JNICALL
Java_com_hmuriy_HmuriyBridge_nativeUiDump(JNIEnv* env, jclass, jstring xml) {
    if (!xml) return;
    const char* chars = env->GetStringUTFChars(xml, nullptr);
    if (!chars) return;
    std::string text(chars);
    env->ReleaseStringUTFChars(xml, chars);
    if (text.size() > 48000u) text.resize(48000u);
    {
        std::lock_guard<std::mutex> lock(g_dump_mu);
        g_pending_dump.swap(text);
    }
    wake_worker();
}

extern "C" JNIEXPORT void JNICALL
Java_com_hmuriy_HmuriyBridge_nativeTapBinary(
        JNIEnv* env,
        jclass,
        jobject ws,
        jobject byte_string,
        jint direction) {
    const uint64_t start = now_ns();

    if (!g_initialized.load(std::memory_order_acquire)
            || !ws
            || !byte_string
            || !ensure_byte_string_methods(env, byte_string)) {
        record_tap(now_ns() - start);
        return;
    }

    jint size = env->CallIntMethod(byte_string, g_bs_size);
    if (env->ExceptionCheck()) {
        env->ExceptionClear();
        record_tap(now_ns() - start);
        return;
    }
    if (size < 3) {
        m_non_coin_binary.fetch_add(1, std::memory_order_relaxed);
        record_tap(now_ns() - start);
        return;
    }

    jbyte first = env->CallByteMethod(byte_string, g_bs_get_byte, 0);
    if (env->ExceptionCheck()) {
        env->ExceptionClear();
        record_tap(now_ns() - start);
        return;
    }
    if ((static_cast<uint8_t>(first) & 0x80u) == 0) {
        m_non_coin_binary.fetch_add(1, std::memory_order_relaxed);
        record_tap(now_ns() - start);
        return;
    }

    if (!ensure_worker_started()) {
        record_tap(now_ns() - start);
        return;
    }

    jint hash = env->CallStaticIntMethod(
        g_system_class, g_identity_hash, ws);
    if (env->ExceptionCheck()) {
        env->ExceptionClear();
        record_tap(now_ns() - start);
        return;
    }
    const uint32_t ws_id = static_cast<uint32_t>(hash);
    if (!m_first_coin_tap_logged.exchange(true, std::memory_order_relaxed)) {
        HLOGI(
            "[tap] first Coin frame ws=%08x dir=%s size=%d first=%02x",
            ws_id,
            direction ? "out" : "in",
            static_cast<int>(size),
            static_cast<unsigned>(static_cast<uint8_t>(first)));
    }
    register_ws_nonblocking(env, ws_id, ws);

    std::unique_lock<std::mutex> lock(g_queue_mu, std::try_to_lock);
    if (!lock.owns_lock()) {
        m_queue_contention.fetch_add(1, std::memory_order_relaxed);
        m_queue_drops.fetch_add(1, std::memory_order_relaxed);
        record_tap(now_ns() - start);
        return;
    }
    if (g_queue.size() >= kQueueMax) {
        m_queue_drops.fetch_add(1, std::memory_order_relaxed);
        record_tap(now_ns() - start);
        return;
    }

    jobject global_bs = env->NewGlobalRef(byte_string);
    if (!global_bs) {
        if (env->ExceptionCheck()) env->ExceptionClear();
        m_queue_drops.fetch_add(1, std::memory_order_relaxed);
        record_tap(now_ns() - start);
        return;
    }

    FrameRef frame;
    frame.seq = g_seq.fetch_add(1, std::memory_order_relaxed) + 1;
    frame.queued_ns = now_ns();
    frame.ws_id = ws_id;
    frame.direction = direction ? 1 : 0;
    frame.byte_string = global_bs;
    g_queue.emplace_back(std::move(frame));
    atomic_max(m_queue_highwater, g_queue.size());

    lock.unlock();
    wake_worker();

    record_tap(now_ns() - start);
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_hmuriy_HmuriyBridge_nativeStats(
        JNIEnv* env, jclass) {
    std::string value = stats_string();
    return env->NewStringUTF(value.c_str());
}

JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM* vm, void*) {
    g_vm = vm;
    JNIEnv* env = nullptr;
    if (vm->GetEnv(reinterpret_cast<void**>(&env), JNI_VERSION_1_6) != JNI_OK
            || !env) {
        return JNI_ERR;
    }

    jclass system_local = env->FindClass("java/lang/System");
    if (!system_local) return JNI_ERR;
    g_system_class = static_cast<jclass>(env->NewGlobalRef(system_local));
    env->DeleteLocalRef(system_local);
    if (!g_system_class) return JNI_ERR;

    g_identity_hash = env->GetStaticMethodID(
        g_system_class, "identityHashCode", "(Ljava/lang/Object;)I");
    if (!g_identity_hash) return JNI_ERR;

    return JNI_VERSION_1_6;
}
