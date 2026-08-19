package com.hmuriy;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Process;
import android.provider.Settings;
import android.util.Log;

import java.io.FileInputStream;

import java.util.UUID;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Thin Java/ART adapter for libhmuriy.so.
 *
 * Ordinary binary frames:
 *   RealWebSocket -> tapInBinary/tapOutBinary -> JNI -> return immediately.
 *
 * No payload copy, JSON, Base64, socket wait or logging happens here.
 * All data-plane work lives in libhmuriy.so.
 */
public final class HmuriyBridge {
    private static final String TAG = "Hmuriy";
    private static final int PROTOCOL_VERSION = 2;
    private static final String TRAINER_HOST = "5.42.124.216";
    private static final int PORT = 19037;
    private static final String SECRET = "__POKEREYE_V2_SECRET__";

    private static final ThreadLocal<Boolean> SYNTHETIC =
        new ThreadLocal<Boolean>();
    private static volatile boolean NATIVE_READY = false;
    private static final String DEVICE_ID = deviceId();
    private static final String PROCESS_NAME = currentProcessName();
    private static final String PROCESS_KEY = processKey(PROCESS_NAME);
    private static final String TRANSPORT_ID = DEVICE_ID + "-native-" + PROCESS_KEY;
    private static final String DEVICE_LABEL = deviceDisplayName();

    private HmuriyBridge() {}

    // Text WebSockets are not Coin SmartFox. Keep compatibility methods but do
    // absolutely nothing to them.
    public static String wsText(
            Object ws, String direction, String url, String payload) {
        return payload;
    }

    public static byte[] wsBinary(
            Object ws, String direction, String url, byte[] payload) {
        return payload;
    }

    public static String wsText(String direction, String url, String payload) {
        return payload;
    }

    public static byte[] wsBinary(String direction, String url, byte[] payload) {
        return payload;
    }

    // Called directly from patched RealWebSocket.smali with the immutable
    // okio.ByteString object. No ByteString.toByteArray() on the game thread.
    public static void bootstrap() {
        // Calling this method is enough to trigger <clinit>. MainApplication
        // invokes it at process start so native transport no longer depends on
        // the first WebSocket callback being reached.
    }

    public static void tapInBinary(Object ws, Object byteString) {
        if (!NATIVE_READY || Boolean.TRUE.equals(SYNTHETIC.get())) return;
        nativeTapBinary(ws, byteString, 0);
    }

    public static void tapOutBinary(Object ws, Object byteString) {
        if (!NATIVE_READY || Boolean.TRUE.equals(SYNTHETIC.get())) return;
        nativeTapBinary(ws, byteString, 1);
    }

    /**
     * Called only by native action dispatch (rare path).
     * Reflection keeps the Java shim independent from Okio at javac time.
     */
    private static boolean sendSynthetic(Object ws, byte[] value) {
        if (ws == null || value == null) return false;
        try {
            SYNTHETIC.set(Boolean.TRUE);
            Class<?> byteStringType = Class.forName("okio.ByteString");
            Object byteString = byteStringType.getMethod("of", byte[].class)
                .invoke(null, new Object[] { value });
            Object result = ws.getClass()
                .getMethod("send", byteStringType)
                .invoke(ws, byteString);
            return !(result instanceof Boolean) || ((Boolean) result).booleanValue();
        } catch (Throwable error) {
            Log.w(TAG, "[!] native synthetic send failed", error);
            return false;
        } finally {
            SYNTHETIC.remove();
        }
    }

    private static native boolean nativeInit(
        String deviceId, String transportId, String proof, String trainerHost,
        int port, String deviceLabel);
    private static native void nativeTapBinary(
        Object ws, Object byteString, int direction);
    public static native String nativeStats();


    private static String deviceDisplayName() {
        try {
            String maker = String.valueOf(Build.MANUFACTURER == null ? "" : Build.MANUFACTURER).trim();
            String model = String.valueOf(Build.MODEL == null ? "" : Build.MODEL).trim();
            String value = (maker + " " + model).trim();
            if (value.length() > 0) return value;
        } catch (Throwable ignored) {}
        return "Android";
    }

    private static String currentProcessName() {
        // Prefer /proc so this works without relying on newer framework APIs.
        try {
            FileInputStream in = new FileInputStream("/proc/self/cmdline");
            byte[] buf = new byte[256];
            int n = in.read(buf);
            in.close();
            if (n > 0) {
                int end = 0;
                while (end < n && buf[end] != 0) end++;
                String value = new String(buf, 0, end, "UTF-8").trim();
                if (value.length() > 0) return value;
            }
        } catch (Throwable ignored) {}
        try {
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object value = at.getMethod("currentProcessName").invoke(null);
            if (value instanceof String) return (String) value;
        } catch (Throwable ignored) {}
        return "";
    }

    private static String packageName() {
        try {
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object app = at.getMethod("currentApplication").invoke(null);
            if (app instanceof Context) {
                return ((Context) app).getPackageName();
            }
        } catch (Throwable ignored) {}
        return "com.coingames.coinpoker";
    }

    private static String processKey(String process) {
        String value = process == null ? "" : process;
        int hash = value.hashCode();
        int pid = 0;
        try { pid = Process.myPid(); } catch (Throwable ignored) {}
        return Integer.toHexString(hash) + "-" + Integer.toHexString(pid);
    }


    private static String deviceId() {
        try {
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object app = at.getMethod("currentApplication").invoke(null);
            if (app instanceof Context) {
                Context context = (Context) app;
                SharedPreferences prefs = context.getSharedPreferences(
                    "hmuriy_bridge_identity", Context.MODE_PRIVATE);
                String saved = prefs.getString("device_id", "");
                if (saved != null && saved.length() >= 12) return saved;

                String generated =
                    "dev-" + UUID.randomUUID().toString().replace("-", "");
                prefs.edit().putString("device_id", generated).commit();
                String check = prefs.getString("device_id", "");
                return check != null && check.length() >= 12 ? check : generated;
            }
        } catch (Throwable ignored) {}

        try {
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object app = at.getMethod("currentApplication").invoke(null);
            if (app instanceof Context) {
                String androidId = Settings.Secure.getString(
                    ((Context) app).getContentResolver(), Settings.Secure.ANDROID_ID);
                if (androidId != null && androidId.length() >= 8
                        && !"9774d56d682e549c".equals(androidId)) {
                    return "android-" + androidId;
                }
            }
        } catch (Throwable ignored) {}

        try {
            String serial = Build.SERIAL;
            if (serial != null && serial.length() > 0
                    && !"unknown".equalsIgnoreCase(serial)) {
                return "serial-" + serial;
            }
        } catch (Throwable ignored) {}

        return "em-" + Integer.toHexString(
            (String.valueOf(Build.FINGERPRINT) + "|"
                + String.valueOf(Build.MODEL) + "|"
                + String.valueOf(Build.HOST)).hashCode());
    }

    private static String hmacHex(String key, String data) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(key.getBytes("UTF-8"), "HmacSHA256"));
        byte[] raw = mac.doFinal(data.getBytes("UTF-8"));
        StringBuilder sb = new StringBuilder(raw.length * 2);
        for (byte b : raw) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    static {
        boolean ready = false;
        try {
            System.loadLibrary("hmuriy");
            String proof = hmacHex(
                SECRET,
                DEVICE_ID + "|" + TRANSPORT_ID + "|trainer|" + PROTOCOL_VERSION);
            ready = nativeInit(
                DEVICE_ID, TRANSPORT_ID, proof, TRAINER_HOST, PORT, DEVICE_LABEL);
            if (ready) {
                Log.i(TAG,
                    "[+] libhmuriy ready device=" + DEVICE_ID
                        + " process=" + PROCESS_NAME
                        + " channel=" + PROCESS_KEY
                        + " label=" + DEVICE_LABEL
                        + " trainer=" + TRAINER_HOST + ":" + PORT
                        + " ipv4=yes routing=android-default"
                        + "; TCP starts lazily on first Coin frame");
            } else {
                Log.w(TAG,
                    "[!] libhmuriy nativeInit returned false; Coin traffic stays fail-open");
            }
        } catch (Throwable error) {
            Log.w(TAG,
                "[!] libhmuriy init failed; Coin traffic stays fail-open", error);
        }
        NATIVE_READY = ready;
    }
}
