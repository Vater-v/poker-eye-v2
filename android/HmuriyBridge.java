/* poker-eye-v2 Android bridge client.
 *
 * v2 extensions over the baseline HmuriyBridge:
 *   - UDP broadcast discovery of the trainer (replaces hardcoded 127.0.0.1:18010).
 *   - Authenticated TCP handshake (hello + HMAC-SHA256 proof Р Р†РІР‚В РІР‚в„ў welcome).
 *   - Per-WebSocket (per-table) TCP connections for multitable readiness.
 *   - Heartbeat every 5 s on idle connections.
 *   - Idle connection tear-down after 60 s.
 *
 * The dispatch / decision / scheduleBinary / inject contract with the trainer
 * is unchanged: every ws_message frame is answered immediately (forward /
 * schedule_send / replace / drop).  Synthetic sends are marked via the
 * SYNTHETIC ThreadLocal flag exactly as in the baseline.
 */
package com.hmuriy;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.util.Base64;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.DataOutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketException;
import java.net.SocketTimeoutException;
import java.nio.charset.Charset;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public final class HmuriyBridge {
    /* ---- constants ---- */
    private static final String TAG = "Hmuriy";
    private static final ConcurrentHashMap<String, Long> STATUS_LAST =
        new ConcurrentHashMap<String, Long>();

    private static void diag(String message) {
        if (PRODUCTION_DIAGNOSTICS) Log.d(TAG, message);
    }

    private static void status(String key, String message, long minIntervalMs) {
        long now = System.currentTimeMillis();
        Long previous = STATUS_LAST.get(key);
        if (previous != null && now - previous.longValue() < minIntervalMs) return;
        STATUS_LAST.put(key, Long.valueOf(now));
        Log.i(TAG, message);
    }

    private static String shortError(Throwable error) {
        Throwable leaf = error;
        while (leaf != null && leaf.getCause() != null && leaf.getCause() != leaf) {
            leaf = leaf.getCause();
        }
        if (leaf == null) return "unknown error";
        String name = leaf.getClass().getSimpleName();
        String message = leaf.getMessage();
        return message == null || message.length() == 0 ? name : name + ": " + message;
    }

    private static boolean isExpectedBridgeState(Throwable error) {
        Throwable current = error;
        while (current != null) {
            if (current instanceof IllegalStateException
                    || current instanceof EOFException
                    || current instanceof SocketException
                    || current instanceof SocketTimeoutException) {
                return true;
            }
            current = current.getCause();
        }
        return false;
    }

    private static final String BOOTSTRAP_HOST = "37.192.228.101";
    private static final int BOOTSTRAP_PORT = 19037;
    private static final int CONNECT_TIMEOUT_MS = 1000;
    private static final int BOOTSTRAP_TIMEOUT_MS = 1000;
    private static final int READ_TIMEOUT_MS = 5000;
    private static final long BOOTSTRAP_BACKOFF_MS = 15000L;
    // Keep production diagnostics off; enable only for targeted field debugging.
    private static final boolean PRODUCTION_DIAGNOSTICS = false;
    private static final int MAX_FRAME = 8 * 1024 * 1024;
    private static final int MAX_INJECT = 32;
    private static final int MAX_SCHEDULE_DELAY_MS = 15000;
    private static final int PROTOCOL_VERSION = 2;
    private static final String SECRET = "__POKEREYE_V2_SECRET__";
    private static final Charset UTF8 = Charset.forName("UTF-8");

    /* ---- shared state ---- */
    private static final Object IO_LOCK = new Object();
    private static final AtomicLong IDS = new AtomicLong();
    private static final ThreadLocal<Boolean> SYNTHETIC = new ThreadLocal<Boolean>();
    private static final ConcurrentHashMap<String, ScheduledFuture<?>> SCHEDULED =
        new ConcurrentHashMap<String, ScheduledFuture<?>>();
    private static final ScheduledThreadPoolExecutor SEND_TIMER =
        new ScheduledThreadPoolExecutor(1, new ThreadFactory() {
            @Override public Thread newThread(Runnable task) {
                Thread t = new Thread(task, "HmuriySendTimer");
                t.setDaemon(true);
                return t;
            }
        });

    /* ---- fixed public trainer state ---- */
    private static volatile String trainerHost = BOOTSTRAP_HOST;
    private static volatile int trainerPort = BOOTSTRAP_PORT;
    private static volatile String trainerNonce = "";
    private static volatile String sessionId = "trainer";
    private static volatile long bootstrapRetryAfter = 0L;

    /* ---- device identity ---- */
    private static final String DEVICE_ID = deviceId();

    /* ---- per-WebSocket connections (wsId hex -> Conn) ---- */
    private static final ConcurrentHashMap<String, Conn> CONNS =
        new ConcurrentHashMap<String, Conn>();
    private static final ConcurrentHashMap<String, Object> CONN_LOCKS =
        new ConcurrentHashMap<String, Object>();

    static class Conn {
        Socket socket;
        DataInputStream in;
        DataOutputStream out;
        volatile long lastUse;
        String tableId;
    }

    /* ---- lifecycle ---- */
    private HmuriyBridge() {}

    /* ================================================================
     * Public API (called by the patched RealWebSocket)
     * ================================================================ */
    public static String wsText(Object ws, String direction, String url, String payload) {
        if (payload == null) return null;
        if (isSynthetic()) return payload;
        byte[] result = dispatch(ws, direction, url, true, payload.getBytes(UTF8));
        return result == null ? null : new String(result, UTF8);
    }

    public static byte[] wsBinary(Object ws, String direction, String url, byte[] payload) {
        if (payload == null) return null;
        if (isSynthetic()) return payload;
        return dispatch(ws, direction, url, false, payload);
    }

    public static String wsText(String direction, String url, String payload) {
        return wsText(null, direction, url, payload);
    }

    public static byte[] wsBinary(String direction, String url, byte[] payload) {
        return wsBinary(null, direction, url, payload);
    }

    /* ================================================================
     * Dispatch
     * ================================================================ */
    private static boolean isSynthetic() {
        return Boolean.TRUE.equals(SYNTHETIC.get());
    }

    private static byte[] dispatch(Object ws, String direction, String url,
                                   boolean text, byte[] payload) {
        if (payload.length > MAX_FRAME) return payload;
        String id = Long.toHexString(System.nanoTime()) + "-" + IDS.incrementAndGet();
        String wsId = ws == null ? "global" :
            Integer.toHexString(System.identityHashCode(ws));
        String tableId = DEVICE_ID + "-" + wsId;
        status("bridge.active", "[+] bridge active; WebSocket traffic detected", 60000L);

        try {
            JSONObject event = new JSONObject();
            event.put("v", 3);
            event.put("schedule_send", true);
            event.put("id", id);
            event.put("kind", "ws_message");
            event.put("direction", direction == null ? "" : direction);
            event.put("text", text);
            event.put("url", url == null ? "" : url);
            event.put("ws_id", wsId);
            event.put("payload_b64", Base64.encodeToString(payload, Base64.NO_WRAP));
            synchronized (IO_LOCK) {
                Conn conn = ensureConn(wsId, tableId);
                conn.lastUse = System.currentTimeMillis();
                byte[] raw = exchangeLocked(conn, event.toString().getBytes(UTF8));
                if (raw == null) return payload;
                JSONObject decision = new JSONObject(new String(raw, UTF8));
                String responseId = decision.optString("id", "");
                if (responseId.length() != 0 && !id.equals(responseId)) return payload;
                cancelScheduled(decision.optString("cancel_schedule", ""));
                applyInjectLocked(ws, decision.optJSONArray("inject"));
                String action = decision.optString("action", "forward");
                if ("drop".equals(action)) return null;
                if ("replace".equals(action)) {
                    String encoded = decision.optString("payload_b64", null);
                    if (encoded == null) return payload;
                    byte[] replacement = Base64.decode(encoded, Base64.DEFAULT);
                    return replacement.length <= MAX_FRAME ? replacement : payload;
                }
                if ("schedule_send".equals(action)) {
                    String encoded = decision.optString("payload_b64", null);
                    if (encoded == null || ws == null) return payload;
                    byte[] replacement = Base64.decode(encoded, Base64.DEFAULT);
                    if (replacement.length > MAX_FRAME) return payload;
                    int delay = Math.max(0,
                        Math.min(MAX_SCHEDULE_DELAY_MS, decision.optInt("delay_ms", 0)));
                    String token = decision.optString("token", id);
                    scheduleBinary(ws, replacement, delay, token);
                    return payload;
                }
                return payload;
            }
        } catch (Throwable error) {
            if (isExpectedBridgeState(error)) {
                String message = shortError(error);
                String key = message.indexOf("bootstrap backoff") >= 0
                    ? "bootstrap.backoff" : "bridge.pending";
                status(key, "[~] bridge waiting: " + message
                    + " (game traffic passes through)", 3000L);
            } else {
                Log.w(TAG, "[!] bridge fail-open (unexpected)", error);
            }
            return payload;
        }
    }

    /* ================================================================
     * Per-WS connection management
     * ================================================================ */
    private static Conn ensureConn(String wsId, String tableId) throws Exception {
        Object lock = CONN_LOCKS.get(wsId);
        if (lock == null) {
            Object fresh = new Object();
            Object prior = CONN_LOCKS.putIfAbsent(wsId, fresh);
            lock = prior == null ? fresh : prior;
        }
        synchronized (lock) {
            return ensureConnUnlocked(wsId, tableId);
        }
    }

    private static Conn ensureConnUnlocked(String wsId, String tableId) throws Exception {
        Conn c = CONNS.get(wsId);
        if (c != null && connected(c)) return c;
        // Bootstrap/discovery is best-effort: a first frame must not wait for
        // reconnect sleeps when no channel has ever been established.
        boolean hadChannel = c != null;
        if (hadChannel) {
            status("reconnect.begin." + wsId, "[~] channel lost; reconnecting table=" + tableId, 1000L);
        }
        if (c != null) closeQuiet(c);
        Throwable last = null;
        int attempts = hadChannel ? 3 : 1;
        // Only an established channel gets the 3x/3s reconnect policy.
        for (int attempt = 1; attempt <= attempts; attempt++) {
            try {
                c = connect(tableId);
                CONNS.put(wsId, c);
                c.tableId = tableId;
                return c;
            } catch (Throwable error) {
                last = error;
                closeQuiet(c);
                if (attempt < attempts) {
                    status("reconnect.try." + wsId,
                        "[~] reconnect " + attempt + "/" + attempts + " failed ("
                        + shortError(error) + "); retry in 3s", 0L);
                    try { Thread.sleep(3000); } catch (InterruptedException e) {
                        Thread.currentThread().interrupt(); break;
                    }
                }
            }
        }
        if (!hadChannel) {
            if (last != null) {
                status("connect.pending." + wsId,
                    "[~] trainer not ready yet: " + shortError(last), 3000L);
            }
            if (last instanceof Exception) throw (Exception) last;
            throw new Exception(last);
        }
        // The assignment may be stale; force a new callback lease, generation,
        // and token rather than reusing the old assignment.
        status("reallocate." + wsId,
            "[~] reconnect failed; requesting a fresh slot", 0L);
        trainerHost = BOOTSTRAP_HOST; trainerPort = BOOTSTRAP_PORT;
        bootstrap(true);
        try {
            c = connect(tableId);
            CONNS.put(wsId, c);
            c.tableId = tableId;
            return c;
        } catch (Throwable error) {
            status("reallocate.fail." + wsId,
                "[!] fresh slot/channel failed: " + shortError(error), 0L);
            throw error;
        }
    }

    private static void bootstrap() throws Exception {
        bootstrap(false);
    }

    private static void bootstrap(boolean forceNewLease) throws Exception {
        long now = System.currentTimeMillis();
        if (now < bootstrapRetryAfter) {
            long remaining = Math.max(1L, bootstrapRetryAfter - now);
            status("bootstrap.backoff",
                "[~] bootstrap backoff; retry in " + ((remaining + 999L) / 1000L) + "s", 3000L);
            throw new IllegalStateException("bootstrap backoff");
        }
        status("bootstrap.search",
            (forceNewLease ? "[*] requesting fresh slot at " : "[*] looking for slot at ")
                + BOOTSTRAP_HOST + ":" + BOOTSTRAP_PORT, 3000L);
        try {
            bootstrapAttempt(forceNewLease);
            bootstrapRetryAfter = 0L;
        } catch (Throwable error) {
            bootstrapRetryAfter = System.currentTimeMillis() + BOOTSTRAP_BACKOFF_MS;
            status("bootstrap.failed",
                "[~] slot lookup failed: " + shortError(error)
                    + "; retry in " + (BOOTSTRAP_BACKOFF_MS / 1000L) + "s", 3000L);
            throw error;
        }
    }

    private static void bootstrapAttempt(boolean forceNewLease) throws Exception {
        Socket s = new Socket();
        s.connect(new InetSocketAddress(BOOTSTRAP_HOST, BOOTSTRAP_PORT), BOOTSTRAP_TIMEOUT_MS);
        s.setSoTimeout(BOOTSTRAP_TIMEOUT_MS);
        DataInputStream in = new DataInputStream(s.getInputStream());
        DataOutputStream out = new DataOutputStream(s.getOutputStream());
        String n = Long.toHexString(System.nanoTime());
        JSONObject hello = new JSONObject();
        hello.put("type", "bootstrap_hello");
        hello.put("version", PROTOCOL_VERSION);
        hello.put("device_id", DEVICE_ID);
        hello.put("local_ipv4", activeIpv4());
        hello.put("nonce", n);
        hello.put("session_id", "bootstrap");
        if (forceNewLease) hello.put("reallocate", true);
        hello.put("proof", hmacHex(SECRET, n + "|" + DEVICE_ID + "|bootstrap|" + PROTOCOL_VERSION));
        byte[] b = hello.toString().getBytes(UTF8);
        out.writeInt(b.length); out.write(b); out.flush();
        int len = in.readInt();
        if (len < 1 || len > MAX_FRAME) throw new IllegalStateException("bad bootstrap reply");
        byte[] raw = new byte[len]; in.readFully(raw);
        JSONObject reply = new JSONObject(new String(raw, UTF8));
        if (!"bootstrap_ok".equals(reply.optString("type")))
            throw new IllegalStateException("bootstrap rejected");
        String replyDevice = reply.optString("device_id", "");
        if (!DEVICE_ID.equals(replyDevice))
            throw new IllegalStateException("bootstrap device mismatch");
        // Fixed endpoint: bootstrap never leases a dynamic callback port.
        trainerHost = BOOTSTRAP_HOST;
        trainerPort = BOOTSTRAP_PORT;
        trainerNonce = reply.optString("nonce", n);
        sessionId = reply.optString("session_id", "trainer");
        try { in.close(); } catch (Throwable ignored) {}
        try { out.close(); } catch (Throwable ignored) {}
        try { s.close(); } catch (Throwable ignored) {}
        status("bootstrap.assigned", "[+] direct public endpoint ready " + trainerHost + ":" + trainerPort, 0L);
    }

    private static Conn connect(String tableId) throws Exception {
        if (trainerHost == null || trainerPort <= 0) {
            bootstrap();
        }
        if (trainerHost == null || trainerPort <= 0)
            throw new IllegalStateException("no trainer discovered");
        status("connect." + tableId,
            "[*] connecting callback=" + trainerHost + ":" + trainerPort
                + " table=" + tableId, 1000L);
        Network net = wifiNetwork();
        Socket s;
        if (net == null) {
            s = new Socket();
        } else {
            try {
                s = net.getSocketFactory().createSocket();
                diag("using Wi-Fi network for trainer TCP");
            } catch (Throwable error) {
                // A Network can exist without permitting app sockets to bind to it.
                // Fall back to the system route; VPN is not required by v2.
                status("connect.network.fallback",
                    "[~] preferred network unavailable; using default route for callback", 15000L);
                s = new Socket();
            }
        }
        try {
            s.connect(new InetSocketAddress(trainerHost, trainerPort), CONNECT_TIMEOUT_MS);
        } catch (Throwable error) {
            closeSocketQuiet(s);
            if (net == null) throw error;
            status("connect.network.fallback", "[~] preferred network rejected callback; retrying on default route", 15000L);
            s = new Socket();
            s.connect(new InetSocketAddress(trainerHost, trainerPort), CONNECT_TIMEOUT_MS);
        }
        s.setSoTimeout(READ_TIMEOUT_MS);
        DataInputStream in = new DataInputStream(s.getInputStream());
        DataOutputStream out = new DataOutputStream(s.getOutputStream());
        JSONObject hello = new JSONObject();
        hello.put("version", PROTOCOL_VERSION);
        hello.put("device_id", DEVICE_ID);
        String proof = hmacHex(SECRET, DEVICE_ID + "|" + tableId + "|" + sessionId + "|" + PROTOCOL_VERSION);
        hello.put("type", "direct_hello");
        hello.put("table_id", tableId);
        hello.put("proof", proof);
        byte[] hb = hello.toString().getBytes(UTF8);
        out.writeInt(hb.length);
        out.write(hb);
        out.flush();
        int len = in.readInt();
        if (len < 1 || len > MAX_FRAME) throw new IllegalStateException("bad welcome frame");
        byte[] resp = new byte[len];
        in.readFully(resp);
        JSONObject welcome = new JSONObject(new String(resp, UTF8));
        String welcomeType = welcome.optString("type");
        if (!"welcome".equals(welcomeType) && !"callback_welcome".equals(welcomeType))
            throw new IllegalStateException("handshake rejected: " + welcome.optString("error", "?"));
        Conn c = new Conn();
        c.socket = s;
        c.in = in;
        c.out = out;
        c.lastUse = System.currentTimeMillis();
        status("connected." + tableId,
            "[+] connected table=" + tableId + " callback="
                + trainerHost + ":" + trainerPort, 0L);
        return c;
    }

    /* ================================================================
     * Exchange (v1 protocol, unchanged)
     * ================================================================ */
    private static byte[] exchangeLocked(Conn c, byte[] request) {
        try {
            c.out.writeInt(request.length);
            c.out.write(request);
            c.out.flush();
            int length = c.in.readInt();
            if (length < 0 || length > MAX_FRAME)
                throw new IllegalStateException("bad frame");
            byte[] response = new byte[length];
            c.in.readFully(response);
            return response;
        } catch (Throwable error) {
            status("channel.drop." + String.valueOf(c.tableId),
                "[~] channel dropped table=" + String.valueOf(c.tableId) + ": "
                    + shortError(error) + "; reconnect on next frame", 1000L);
            closeQuiet(c);
            return null;
        }
    }

    private static void closeSocketQuiet(Socket s) {
        try { if (s != null) s.close(); } catch (Throwable ignored) {}
    }
    private static boolean connected(Conn c) {
        if (c == null || c.socket == null) return false;
        return c.socket.isConnected() && !c.socket.isClosed();
    }

    private static void closeQuiet(Conn c) {
        if (c == null) return;
        try { if (c.in != null) c.in.close(); } catch (Throwable ignored) {}
        try { if (c.out != null) c.out.close(); } catch (Throwable ignored) {}
        try { if (c.socket != null) c.socket.close(); } catch (Throwable ignored) {}
        c.socket = null; c.in = null; c.out = null;
    }

    /* ================================================================
     * Injection & scheduling (v1, unchanged except no static socket)
     * ================================================================ */
    private static void cancelScheduled(String token) {
        if (token == null || token.length() == 0) return;
        ScheduledFuture<?> future = SCHEDULED.remove(token);
        if (future != null) future.cancel(false);
    }

    private static void scheduleBinary(final Object ws, final byte[] value,
                                       int delayMs, final String token) {
        cancelScheduled(token);
        ScheduledFuture<?> future = SEND_TIMER.schedule(new Runnable() {
            @Override public void run() {
                try {
                    SYNTHETIC.set(Boolean.TRUE);
                    sendBinaryReflective(ws, value);
                } catch (Throwable error) {
                    Log.w(TAG, "scheduled websocket send failed", error);
                } finally {
                    SYNTHETIC.remove();
                    SCHEDULED.remove(token);
                }
            }
        }, Math.max(1, delayMs), TimeUnit.MILLISECONDS);
        SCHEDULED.put(token, future);
    }

    private static void applyInjectLocked(Object ws, JSONArray commands) {
        if (ws == null || commands == null) return;
        int count = Math.min(commands.length(), MAX_INJECT);
        for (int i = 0; i < count; i++) {
            try {
                JSONObject command = commands.optJSONObject(i);
                if (command == null) continue;
                String encoded = command.optString("payload_b64", null);
                if (encoded == null) continue;
                byte[] bytes = Base64.decode(encoded, Base64.DEFAULT);
                if (bytes.length > MAX_FRAME) continue;
                SYNTHETIC.set(Boolean.TRUE);
                if (command.optBoolean("text", true)) {
                    sendTextReflective(ws, new String(bytes, UTF8));
                } else {
                    sendBinaryReflective(ws, bytes);
                }
            } catch (Throwable error) {
                Log.w(TAG, "synthetic websocket send failed", error);
            } finally {
                SYNTHETIC.remove();
            }
        }
    }

    private static void sendTextReflective(Object ws, String value) throws Exception {
        ws.getClass().getMethod("send", String.class).invoke(ws, value);
    }

    private static void sendBinaryReflective(Object ws, byte[] value) throws Exception {
        Class<?> type = Class.forName("okio.ByteString");
        Object byteString = type.getMethod("of", byte[].class)
            .invoke(null, new Object[] { value });
        ws.getClass().getMethod("send", type).invoke(ws, byteString);
    }

    /* ================================================================
     * Heartbeat (every 5 s on idle connections)
     * ================================================================ */
    private static final Runnable HEARTBEAT = new Runnable() {
        @Override public void run() {
            while (true) {
                try { Thread.sleep(5000); } catch (InterruptedException e) { return; }
                long now = System.currentTimeMillis();
                for (Map.Entry<String, Conn> e : CONNS.entrySet()) {
                    Conn c = e.getValue();
                    if (c == null || !connected(c)) {
                        closeQuiet(c);
                        CONNS.remove(e.getKey(), c);
                        continue;
                    }
                    if (now - c.lastUse < 5000) continue; // traffic is alive
                    synchronized (IO_LOCK) {
                        try {
                            JSONObject hb = new JSONObject();
                            hb.put("type", "heartbeat");
                            hb.put("sequence", now);
                            byte[] b = hb.toString().getBytes(UTF8);
                            c.out.writeInt(b.length);
                            c.out.write(b);
                            c.out.flush();
                            int len = c.in.readInt();
                            if (len < 0 || len > MAX_FRAME)
                                throw new IllegalStateException("bad heartbeat frame");
                            byte[] resp = new byte[len];
                            c.in.readFully(resp);
                            c.lastUse = now;
                        } catch (Throwable err) {
                            closeQuiet(c);
                            CONNS.remove(e.getKey(), c);
                        }
                    }
                }
            }
        }
    };

    /* ================================================================
     * Idle connection sweep (60 s)
     * ================================================================ */
    private static final Runnable IDLE_SWEEP = new Runnable() {
        @Override public void run() {
            while (true) {
                try { Thread.sleep(15000); } catch (InterruptedException e) { return; }
                long now = System.currentTimeMillis();
                for (Map.Entry<String, Conn> e : CONNS.entrySet()) {
                    Conn c = e.getValue();
                    if (c != null && now - c.lastUse > 60000) {
                        synchronized (IO_LOCK) {
                            closeQuiet(c);
                            CONNS.remove(e.getKey(), c);
                        }
                    }
                }
            }
        }
    };

    /* ================================================================
     * Helpers
     * ================================================================ */
    private static String deviceId() {
        try {
            String s = Build.SERIAL;
            if (s != null && s.length() > 0) return s;
        } catch (Throwable ignored) {}
        return "em-" + Integer.toHexString(
            (Build.MODEL == null ? "" : Build.MODEL).hashCode());
    }

    private static String activeIpv4() {
        try {
            Network net = wifiNetwork();
            if (net != null) {
                java.net.NetworkInterface ni = java.net.NetworkInterface.getByName("wlan0");
                if (ni != null) {
                    java.util.Enumeration<java.net.InetAddress> addrs = ni.getInetAddresses();
                    while (addrs.hasMoreElements()) {
                        java.net.InetAddress a = addrs.nextElement();
                        if (a instanceof java.net.Inet4Address && !a.isLoopbackAddress())
                            return a.getHostAddress();
                    }
                }
            }
            java.util.Enumeration<java.net.NetworkInterface> all = java.net.NetworkInterface.getNetworkInterfaces();
            while (all != null && all.hasMoreElements()) {
                java.net.NetworkInterface ni = all.nextElement();
                if (!ni.isUp() || ni.isLoopback() || ni.getName().startsWith("tun")) continue;
                java.util.Enumeration<java.net.InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    java.net.InetAddress a = addrs.nextElement();
                    if (a instanceof java.net.Inet4Address && !a.isLoopbackAddress())
                        return a.getHostAddress();
                }
            }
        } catch (Throwable error) {
            if (PRODUCTION_DIAGNOSTICS) Log.w(TAG, "active IPv4 lookup failed", error);
        }
        return "0.0.0.0";
    }

    private static Network wifiNetwork() {
        try {
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object app = at.getMethod("currentApplication").invoke(null);
            if (!(app instanceof Context)) return null;
            ConnectivityManager cm = (ConnectivityManager)
                ((Context) app).getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) return null;
            Network[] networks = cm.getAllNetworks();
            for (Network n : networks) {
                NetworkCapabilities caps = cm.getNetworkCapabilities(n);
                if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                        && !caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                    return n;
                }
            }
        } catch (Throwable error) {
            Log.w(TAG, "wifi network lookup failed", error);
        }
        return null;
    }

    private static String hmacHex(String key, String data) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        SecretKeySpec spec = new SecretKeySpec(key.getBytes("UTF-8"), "HmacSHA256");
        mac.init(spec);
        byte[] raw = mac.doFinal(data.getBytes("UTF-8"));
        StringBuilder sb = new StringBuilder();
        for (byte b : raw) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    /* ---- static initialiser (after all field definitions) ---- */
    static {
        Log.i(TAG, "[+] bridge loaded; fixed public transport/heartbeat workers started");
        Thread hb = new Thread(HEARTBEAT, "HmuriyHeartbeat");
        hb.setDaemon(true);
        hb.start();
        Thread sweep = new Thread(IDLE_SWEEP, "HmuriyIdleSweep");
        sweep.setDaemon(true);
        sweep.start();
    }
}
