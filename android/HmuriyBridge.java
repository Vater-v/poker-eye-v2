/* poker-eye-v2 Android bridge client.
 *
 * v2 extensions over the baseline HmuriyBridge:
 *   - UDP broadcast discovery of the trainer (replaces hardcoded 127.0.0.1:18010).
 *   - Authenticated TCP handshake (hello + HMAC-SHA256 proof → welcome).
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
import java.io.DataOutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.net.Socket;
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
    private static final int BROADCAST_PORT = 37020;
    private static final int CONNECT_TIMEOUT_MS = 120;
    private static final int READ_TIMEOUT_MS = 220;
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

    /* ---- discovery state (updated by the discovery thread) ---- */
    private static volatile String trainerHost;
    private static volatile int trainerPort;
    private static volatile String trainerNonce = "";
    private static volatile String sessionId = "trainer";

    /* ---- device identity ---- */
    private static final String DEVICE_ID = deviceId();

    /* ---- per-WebSocket connections (wsId hex -> Conn) ---- */
    private static final ConcurrentHashMap<String, Conn> CONNS =
        new ConcurrentHashMap<String, Conn>();

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
            Log.w(TAG, "bridge fail-open", error);
            return payload;
        }
    }

    /* ================================================================
     * Per-WS connection management
     * ================================================================ */
    private static Conn ensureConn(String wsId, String tableId) throws Exception {
        Conn c = CONNS.get(wsId);
        if (c != null && connected(c)) return c;
        if (c != null) closeQuiet(c);
        c = connect(tableId);
        CONNS.put(wsId, c);
        c.tableId = tableId;
        return c;
    }

    private static Conn connect(String tableId) throws Exception {
        if (trainerHost == null || trainerPort <= 0)
            throw new IllegalStateException("no trainer discovered");
        Log.d(TAG, "connecting " + trainerHost + ":" + trainerPort + " table=" + tableId);
        Network net = wifiNetwork();
        Socket s = net == null ? new Socket() : net.getSocketFactory().createSocket();
        if (net != null) Log.d(TAG, "using Wi-Fi network for trainer TCP");
        s.connect(new InetSocketAddress(trainerHost, trainerPort), CONNECT_TIMEOUT_MS);
        s.setSoTimeout(READ_TIMEOUT_MS);
        DataInputStream in = new DataInputStream(s.getInputStream());
        DataOutputStream out = new DataOutputStream(s.getOutputStream());
        String proof = hmacHex(SECRET, trainerNonce + "|" + tableId + "|" + sessionId + "|" + PROTOCOL_VERSION);
        JSONObject hello = new JSONObject();
        hello.put("type", "hello");
        hello.put("version", PROTOCOL_VERSION);
        hello.put("device_id", DEVICE_ID);
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
        if (!"welcome".equals(welcome.optString("type")))
            throw new IllegalStateException("handshake rejected: " + welcome.optString("error", "?"));
        Conn c = new Conn();
        c.socket = s;
        c.in = in;
        c.out = out;
        c.lastUse = System.currentTimeMillis();
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
            closeQuiet(c);
            return null;
        }
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
     * Discovery (UDP broadcast listener)
     * ================================================================ */
    private static final Runnable DISCOVERY = new Runnable() {
        @Override public void run() {
            DatagramSocket sock = null;
            try {
                sock = new DatagramSocket(null);
                sock.setReuseAddress(true);
                Network net = wifiNetwork();
                if (net != null) {
                    net.bindSocket(sock);
                    Log.d(TAG, "discovery bound to Wi-Fi network " + net);
                }
                sock.bind(new InetSocketAddress(BROADCAST_PORT));
                Log.d(TAG, "discovery UDP bound port=" + BROADCAST_PORT);
                sock.setSoTimeout(1000);
            } catch (Throwable error) {
                Log.w(TAG, "discovery socket bind failed", error);
                return;
            }
            byte[] buf = new byte[2048];
            try {
                DatagramPacket pkt = new DatagramPacket(buf, buf.length);
                while (true) {
                    try {
                        pkt.setData(buf);
                        sock.receive(pkt);
                    } catch (java.net.SocketTimeoutException e) {
                        continue;
                    }
                    int len = pkt.getLength();
                    if (len <= 0) continue;
                    try {
                        JSONObject ad = new JSONObject(
                            new String(pkt.getData(), pkt.getOffset(), len, UTF8));
                        if (!"trainer".equals(ad.optString("type"))) continue;
                        if (ad.optInt("version", 0) != PROTOCOL_VERSION) continue;
                        int port = ad.optInt("tcp_port", 0);
                        if (port <= 0 || port > 65535) continue;
                        String host = ad.optString("host", "");
                        if (host.length() == 0 || "0.0.0.0".equals(host))
                            host = pkt.getAddress().getHostAddress();
                        trainerHost = host;
                        trainerPort = port;
                        trainerNonce = ad.optString("nonce", "");
                        sessionId = ad.optString("session_id", "trainer");
                        Log.d(TAG, "trainer discovered " + trainerHost + ":" + trainerPort);
                    } catch (Throwable ignored) {
                    }
                }
            } catch (Throwable error) {
                Log.w(TAG, "discovery stopped", error);
            } finally {
                try { sock.close(); } catch (Throwable ignored) {}
            }
        }
    };

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
                        CONNS.remove(e.getKey());
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
                            CONNS.remove(e.getKey());
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
                            CONNS.remove(e.getKey());
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
        Thread disc = new Thread(DISCOVERY, "HmuriyDiscovery");
        disc.setDaemon(true);
        disc.start();
        Thread hb = new Thread(HEARTBEAT, "HmuriyHeartbeat");
        hb.setDaemon(true);
        hb.start();
        Thread sweep = new Thread(IDLE_SWEEP, "HmuriyIdleSweep");
        sweep.setDaemon(true);
        sweep.start();
    }
}