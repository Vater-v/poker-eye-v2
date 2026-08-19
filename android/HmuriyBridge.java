package com.hmuriy;

import android.app.Activity;
import android.app.Application;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Rect;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.Log;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.widget.TextView;

import java.io.FileInputStream;
import java.lang.ref.WeakReference;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
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
    private static volatile WeakReference<Activity> CURRENT_ACTIVITY =
        new WeakReference<Activity>(null);
    private static volatile boolean UI_JANITOR_STARTED = false;
    // Coin keeps TABLE CLOSED tabs after protocol quit_table. Janitor walks
    // overflow ("...") -> leave-table -> confirm. One tap per 700ms sweep.
    private static long JANITOR_CLOSED_SEEN_MS = 0L;
    private static long JANITOR_LAST_TAP_MS = 0L;
    private static int JANITOR_OVERFLOW_ROT = 0;
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
        bootstrap(null);
    }

    public static void bootstrap(Context context) {
        // <clinit> already started nativeInit. Attach a UI janitor so Coin
        // dialogs and leftover TABLE CLOSED tabs cannot block protocol automation.
        if (context == null) return;
        try {
            startUiJanitor(context.getApplicationContext());
        } catch (Throwable ignored) {}
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

    private static synchronized void startUiJanitor(Context context) {
        if (UI_JANITOR_STARTED || context == null) return;
        Context appCtx = context.getApplicationContext();
        if (!(appCtx instanceof Application)) return;
        Application app = (Application) appCtx;
        app.registerActivityLifecycleCallbacks(new Application.ActivityLifecycleCallbacks() {
            @Override public void onActivityResumed(Activity activity) {
                CURRENT_ACTIVITY = new WeakReference<Activity>(activity);
            }
            @Override public void onActivityPaused(Activity activity) {
                Activity cur = CURRENT_ACTIVITY.get();
                if (cur == activity) CURRENT_ACTIVITY = new WeakReference<Activity>(null);
            }
            @Override public void onActivityCreated(Activity a, android.os.Bundle b) {}
            @Override public void onActivityStarted(Activity a) {}
            @Override public void onActivityStopped(Activity a) {}
            @Override public void onActivitySaveInstanceState(Activity a, android.os.Bundle b) {}
            @Override public void onActivityDestroyed(Activity a) {}
        });
        final Handler handler = new Handler(Looper.getMainLooper());
        handler.post(new Runnable() {
            @Override public void run() {
                try { sweepBlockingUi(); } catch (Throwable ignored) {}
                handler.postDelayed(this, 700);
            }
        });
        UI_JANITOR_STARTED = true;
        Log.i(TAG, "[+] UI janitor attached");
    }

    private static void sweepBlockingUi() {
        Activity activity = CURRENT_ACTIVITY.get();
        if (activity == null || activity.isFinishing()) return;
        View root = activity.getWindow() == null ? null : activity.getWindow().getDecorView();
        if (root == null) return;
        List<UiNode> nodes = new ArrayList<UiNode>();
        collectUiNodes(root, nodes);
        if (nodes.isEmpty()) return;

        long now = SystemClock.uptimeMillis();
        if (now - JANITOR_LAST_TAP_MS < 650L) return;

        UiNode confirmTitle = null;
        UiNode yesBtn = null;
        UiNode leaveTable = null;
        UiNode goToTables = null;
        UiNode closedTab = null;
        UiNode overflowGlyph = null;
        boolean closedEvidence = false;
        boolean maxTablesVisible = false;
        boolean dangerousConfirm = false;
        int screenW = root.getWidth();
        int screenH = root.getHeight();
        if (screenW <= 0 || screenH <= 0) {
            screenW = activity.getResources().getDisplayMetrics().widthPixels;
            screenH = activity.getResources().getDisplayMetrics().heightPixels;
        }

        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (isDangerousConfirm(node.compact)) dangerousConfirm = true;
            if (isExitTableConfirm(node.compact) && confirmTitle == null) confirmTitle = node;
            if (isConfirmYes(node.compact) && yesBtn == null) yesBtn = node;
            if (isLeaveTableMenu(node.compact) && leaveTable == null) leaveTable = node;
            if (isGoToMyTables(node.compact) && goToTables == null) goToTables = node;
            if (isMaxTables(node.compact)) maxTablesVisible = true;
            if (isMaxTables(node.compact) || isTableClosed(node.compact)) closedEvidence = true;
            if (isTableClosed(node.compact) && closedTab == null) closedTab = node;
            if (isOverflowGlyph(node.text, node.compact) && isTopLeft(node, screenW, screenH)
                    && overflowGlyph == null) {
                overflowGlyph = node;
            }
        }

        // TABLE CLOSED felt is often an image; the overlay still exposes
        // "Go to my tables" as Text. That is closed-tab evidence, not a leave.
        if (goToTables != null && !maxTablesVisible) closedEvidence = true;
        if (closedEvidence) JANITOR_CLOSED_SEEN_MS = now;
        boolean wantClose = closedEvidence || (now - JANITOR_CLOSED_SEEN_MS) < 12000L;

        // Never confirm logout / exit-app / KYC. Table-exit confirm is the
        // last step of overflow -> leave table -> yes.
        if (confirmTitle != null && !dangerousConfirm) {
            View tap = yesBtn != null ? clickableOf(yesBtn.view) : rightConfirmButton(nodes, screenW, screenH);
            if (tapView(tap, "confirm-exit-table")) {
                JANITOR_CLOSED_SEEN_MS = 0L;
                JANITOR_OVERFLOW_ROT = 0;
            }
            return;
        }
        if (leaveTable != null) {
            tapView(clickableOf(leaveTable.view), "leave-table");
            return;
        }
        // Only the Maximum Tables Opened modal. TABLE CLOSED overlays also
        // show Go to my tables and tapping it would starve the leave path.
        if (goToTables != null && maxTablesVisible) {
            tapView(clickableOf(goToTables.view), "go-to-my-tables");
            return;
        }
        if (closedTab != null && clickableOf(closedTab.view) != null
                && (now - JANITOR_LAST_TAP_MS) > 1200L) {
            tapView(clickableOf(closedTab.view), "focus-closed-tab");
            return;
        }
        if (wantClose && overflowGlyph != null) {
            tapView(clickableOf(overflowGlyph.view), "overflow-glyph");
            return;
        }
        if (wantClose) {
            View overflow = pickTopLeftOverflow(nodes, screenW, screenH);
            if (overflow != null) tapView(overflow, "overflow-topleft");
        }
    }

    private static final class UiNode {
        final View view;
        final String text;
        final String compact;
        final Rect bounds;
        UiNode(View view, String text, String compact, Rect bounds) {
            this.view = view;
            this.text = text;
            this.compact = compact;
            this.bounds = bounds;
        }
    }

    private static void collectUiNodes(View view, List<UiNode> out) {
        if (view == null || !view.isShown()) return;
        String text = viewLabel(view);
        if (text.length() > 0 || view.isClickable()) {
            Rect bounds = new Rect();
            if (view.getGlobalVisibleRect(bounds) && !bounds.isEmpty()) {
                out.add(new UiNode(view, text, compact(text), bounds));
            }
        }
        if (view instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) view;
            int n = group.getChildCount();
            for (int i = 0; i < n; i++) collectUiNodes(group.getChildAt(i), out);
        }
    }

    private static String viewLabel(View view) {
        CharSequence raw = null;
        if (view instanceof TextView) {
            raw = ((TextView) view).getText();
            if (raw == null || raw.length() == 0) raw = ((TextView) view).getHint();
        }
        if (raw == null || raw.length() == 0) raw = view.getContentDescription();
        if ((raw == null || raw.length() == 0) && Build.VERSION.SDK_INT >= 26) {
            try { raw = view.getTooltipText(); } catch (Throwable ignored) {}
        }
        if (raw == null) return "";
        return raw.toString().replace('\n', ' ').trim();
    }

    private static String compact(String text) {
        if (text == null) return "";
        StringBuilder sb = new StringBuilder(text.length());
        for (int i = 0; i < text.length(); i++) {
            char ch = text.charAt(i);
            if (ch <= 32) continue;
            if (ch == '?' || ch == '!' || ch == '.' || ch == ',' || ch == ':'
                    || ch == '\'' || ch == '"' || ch == '\u2026') continue;
            sb.append(Character.toLowerCase(ch));
        }
        return sb.toString();
    }

    private static boolean isGoToMyTables(String c) {
        return c.contains("gotomytables")
                || c.contains("\u043f\u0435\u0440\u0435\u0439\u0442\u0438\u043a\u043c\u043e\u0438\u043c\u0441\u0442\u043e\u043b\u0430\u043c");
    }

    private static boolean isMaxTables(String c) {
        return c.contains("maximumtablesopened") || c.contains("maximumtables");
    }

    private static boolean isTableClosed(String c) {
        return c.contains("tableclosed")
                || c.contains("\u044d\u0442\u043e\u0442\u0441\u0442\u043e\u043b\u0437\u0430\u043a\u0440\u044b\u0442")
                || c.contains("\u0441\u0442\u043e\u043b\u0437\u0430\u043a\u0440\u044b\u0442")
                || c.contains("thistableisclosed");
    }

    private static boolean isDangerousConfirm(String c) {
        return c.contains("exitthecoinpoker")
                || c.contains("exitcoinpoker")
                || c.contains("\u0432\u044b\u0439\u0442\u0438\u0438\u0437\u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u044f")
                || c.contains("wanttologout")
                || c.contains("areyousureyouwanttologout")
                || c.contains("\u0445\u043e\u0442\u0438\u0442\u0435\u0432\u044b\u0439\u0442\u0438\u0438\u0437")
                || c.equals("\u0432\u044b\u0443\u0432\u0435\u0440\u0435\u043d\u044b\u0447\u0442\u043e\u0445\u043e\u0442\u0438\u0442\u0435\u0432\u044b\u0439\u0442\u0438")
                || c.contains("exitkyc")
                || c.contains("fantasycricket");
    }

    private static boolean isExitTableConfirm(String c) {
        if (isDangerousConfirm(c)) return false;
        if (c.contains("areyousureyouwanttoexittable")) return true;
        if (c.contains("\u0432\u044b\u0443\u0432\u0435\u0440\u0435\u043d\u044b\u0447\u0442\u043e\u0445\u043e\u0442\u0438\u0442\u0435\u043f\u043e\u043a\u0438\u043d\u0443\u0442\u044c\u0441\u0442\u043e\u043b")) return true;
        return c.contains("\u0443\u0432\u0435\u0440\u0435\u043d\u044b")
                && c.contains("\u043f\u043e\u043a\u0438\u043d\u0443\u0442\u044c\u0441\u0442\u043e\u043b");
    }

    private static boolean isLeaveSeatMenu(String c) {
        return c.contains("\u043f\u043e\u043a\u0438\u043d\u0443\u0442\u044c\u043c\u0435\u0441\u0442\u043e")
                || c.contains("leaveyourseat")
                || c.equals("leaveseat")
                || c.contains("\u0445\u043e\u0442\u0438\u0442\u0435\u043f\u043e\u043a\u0438\u043d\u0443\u0442\u044c\u0441\u0432\u043e\u043c\u0435\u0441\u0442\u043e");
    }

    private static boolean isLeaveTableMenu(String c) {
        if (isLeaveSeatMenu(c)) return false;
        return c.contains("\u043f\u043e\u043a\u0438\u043d\u0443\u0442\u044c\u0441\u0442\u043e\u043b\u0438\u0432\u044b\u0439\u0442\u0438")
                || c.contains("\u043f\u043e\u043a\u0438\u043d\u0443\u0442\u044c\u0438\u0437\u0430\u043a\u0440\u044b\u0442\u044c\u0441\u0442\u043e\u043b")
                || c.contains("leaveandexittable")
                || c.equals("exittable")
                || c.equals("leavetable")
                || c.equals("\u043f\u043e\u043a\u0438\u043d\u0443\u0442\u044c\u0441\u0442\u043e\u043b");
    }

    private static boolean isConfirmYes(String c) {
        return c.equals("yes") || c.equals("\u0434\u0430") || c.equals("ok")
                || c.equals("okay") || c.equals("confirm") || c.equals("\u043e\u043a");
    }

    private static boolean isOverflowGlyph(String text, String compact) {
        String trimmed = text == null ? "" : text.trim();
        return trimmed.equals("...") || trimmed.equals("\u2026")
                || trimmed.equals("\u2022\u2022\u2022")
                || trimmed.equals("\u22ee") || trimmed.equals("\u22ef")
                || compact.equals("menu") || compact.contains("moreoptions")
                || compact.contains("overflow");
    }

    private static boolean isTopLeft(UiNode node, int screenW, int screenH) {
        if (node == null || node.bounds == null) return false;
        int cx = node.bounds.centerX();
        int cy = node.bounds.centerY();
        return cx <= screenW * 22 / 100 && cy <= screenH * 18 / 100;
    }

    private static View clickableOf(View view) {
        View cur = view;
        while (cur != null && !cur.isClickable()) {
            Object parent = cur.getParent();
            cur = parent instanceof View ? (View) parent : null;
        }
        return cur != null && cur.isClickable() && cur.isShown() ? cur : view;
    }

    private static View rightConfirmButton(List<UiNode> nodes, int screenW, int screenH) {
        View best = null;
        int bestX = -1;
        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (node.bounds.centerY() < screenH * 45 / 100) continue;
            if (node.bounds.height() > screenH * 20 / 100) continue;
            String c = node.compact;
            if (c.equals("no") || c.equals("\u043d\u0435\u0442") || c.equals("cancel")
                    || c.equals("\u043e\u0442\u043c\u0435\u043d\u0430")
                    || c.contains("\u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c")) continue;
            View click = clickableOf(node.view);
            if (click == null || !click.isShown()) continue;
            int x = node.bounds.centerX();
            if (x > bestX && x > screenW * 45 / 100) {
                bestX = x;
                best = node.view;
            }
        }
        return best;
    }

    private static View pickTopLeftOverflow(List<UiNode> nodes, int screenW, int screenH) {
        List<View> cands = new ArrayList<View>();
        List<Integer> xs = new ArrayList<Integer>();
        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (!isTopLeft(node, screenW, screenH)) continue;
            if (node.bounds.width() > screenW * 24 / 100) continue;
            if (node.bounds.height() > screenH * 16 / 100) continue;
            if (node.bounds.width() < 12 || node.bounds.height() < 12) continue;
            if (isLeaveTableMenu(node.compact) || isGoToMyTables(node.compact)
                    || isConfirmYes(node.compact) || isLeaveSeatMenu(node.compact)) continue;
            if (node.compact.contains("fold") || node.compact.contains("check")
                    || node.compact.contains("call") || node.compact.contains("raise")) continue;
            View click = clickableOf(node.view);
            if (click == null || !click.isShown() || cands.contains(click)) continue;
            cands.add(click);
            xs.add(Integer.valueOf(node.bounds.left));
        }
        if (cands.isEmpty()) return null;
        List<Integer> order = new ArrayList<Integer>();
        for (int i = 0; i < cands.size(); i++) order.add(Integer.valueOf(i));
        Collections.sort(order, new Comparator<Integer>() {
            @Override public int compare(Integer a, Integer b) {
                return xs.get(a.intValue()).compareTo(xs.get(b.intValue()));
            }
        });
        int idx = Math.abs(JANITOR_OVERFLOW_ROT) % order.size();
        JANITOR_OVERFLOW_ROT++;
        return cands.get(order.get(idx).intValue());
    }

    private static boolean tapView(View target, String why) {
        if (target == null || !target.isShown()) return false;
        JANITOR_LAST_TAP_MS = SystemClock.uptimeMillis();
        boolean clicked = false;
        try { clicked = target.performClick(); } catch (Throwable ignored) {}
        if (!clicked) {
            try {
                int[] loc = new int[2];
                target.getLocationOnScreen(loc);
                float x = loc[0] + Math.max(1, target.getWidth()) / 2f;
                float y = loc[1] + Math.max(1, target.getHeight()) / 2f;
                View root = target.getRootView();
                long t = SystemClock.uptimeMillis();
                MotionEvent down = MotionEvent.obtain(t, t, MotionEvent.ACTION_DOWN, x, y, 0);
                MotionEvent up = MotionEvent.obtain(t, t + 40L, MotionEvent.ACTION_UP, x, y, 0);
                if (root != null) {
                    root.dispatchTouchEvent(down);
                    root.dispatchTouchEvent(up);
                } else {
                    target.dispatchTouchEvent(down);
                    target.dispatchTouchEvent(up);
                }
                down.recycle();
                up.recycle();
                clicked = true;
            } catch (Throwable ignored) {}
        }
        if (clicked) {
            Log.i(TAG, "[+] UI janitor tap " + why);
        }
        return clicked;
    }

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
                        + "; TCP starts at process bootstrap");
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
