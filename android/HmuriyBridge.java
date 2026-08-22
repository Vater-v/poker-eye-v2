package com.hmuriy;

import android.app.Activity;
import android.app.Application;
import android.app.Instrumentation;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.Log;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.FileInputStream;
import java.lang.ref.WeakReference;
import java.lang.reflect.Field;
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
    // NL socat TCP:19037 -> 5.42.124.216:19037. Used after a 3s welcome timeout.
    private static final String TRAINER_FALLBACK = "84.32.231.194";
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
    // LEAVE_STEP is armed only while the focused felt is a closed table.
    // It must never survive a switch onto a live table (that would sit us out).
    private static long JANITOR_LAST_TAP_MS = 0L;
    private static long LAST_TURN_TAB_MS = 0L;
    private static long LAST_LOBBY_TAP_MS = 0L;
    private static float LAST_FOCUS_TIMER = 9999f;
    private static int LAST_FOCUS_TAB_X = -9999;
    private static int JANITOR_OVERFLOW_ROT = 0;
    private static int LEAVE_STEP = 0;
    private static long LEAVE_STEP_MS = 0L;
    private static String LAST_TAP_WHY = "";
    private static long LAST_TAP_AT_MS = 0L;
    private static int LAST_TAP_X = 0;
    private static int LAST_TAP_Y = 0;
    private static boolean LAST_TAP_HIT = false;
    private static volatile boolean LEAVE_STICKY = false;
    private static volatile boolean JOIN_ARMED = false;
    private static volatile double JOIN_BB = 0.02;
    private static volatile HudLayer HUD_LAYER = null;
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
        String trainerFallback, int port, String deviceLabel);
    private static native void nativeTapBinary(
        Object ws, Object byteString, int direction);
    public static native String nativeStats();
    private static native void nativeUiDump(String xml);
    private static long LAST_DUMP_MS = 0L;

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

    private static void clearLeave(String why) {
        if (LEAVE_STEP != 0) {
            Log.i(TAG, "[~] UI janitor abort leave step=" + LEAVE_STEP + " " + why);
        }
        LEAVE_STEP = 0;
        LEAVE_STEP_MS = 0L;
        JANITOR_OVERFLOW_ROT = 0;
    }

    private static void armLeave(int step, long now) {
        LEAVE_STEP = step;
        LEAVE_STEP_MS = now;
    }

    private static boolean leaveArmed(int minStep, long now) {
        return LEAVE_STEP >= minStep && now - LEAVE_STEP_MS < 5000L;
    }

    @SuppressWarnings("unchecked")
    private static List<View> allWindowRoots() {
        List<View> roots = new ArrayList<View>();
        try {
            Class<?> wmg = Class.forName("android.view.WindowManagerGlobal");
            Object inst = wmg.getMethod("getInstance").invoke(null);
            Field field = wmg.getDeclaredField("mViews");
            field.setAccessible(true);
            Object raw = field.get(inst);
            if (raw instanceof List) {
                List<?> list = (List<?>) raw;
                for (int i = 0; i < list.size(); i++) {
                    Object row = list.get(i);
                    if (row instanceof View) roots.add((View) row);
                }
            }
        } catch (Throwable ignored) {}
        Activity activity = CURRENT_ACTIVITY.get();
        if (activity != null && !activity.isFinishing() && activity.getWindow() != null) {
            View decor = activity.getWindow().getDecorView();
            if (decor != null && !roots.contains(decor)) roots.add(decor);
        }
        return roots;
    }

    private static void sweepBlockingUi() {
        List<View> roots = allWindowRoots();
        if (roots.isEmpty()) return;
        List<UiNode> nodes = new ArrayList<UiNode>();
        for (int r = 0; r < roots.size(); r++) collectUiNodes(roots.get(r), nodes);
        if (nodes.isEmpty()) return;
        View root = roots.get(roots.size() - 1);

        long now = SystemClock.uptimeMillis();
        if (now - JANITOR_LAST_TAP_MS < 650L) return;
        if (LEAVE_STEP != 0 && now - LEAVE_STEP_MS > 5000L) clearLeave("timeout");

        UiNode confirmTitle = null;
        UiNode yesBtn = null;
        UiNode leaveTable = null;
        UiNode goToTables = null;
        UiNode closedChip = null;
        UiNode overflowGlyph = null;
        UiNode turnTab = null;
        float turnTabSeconds = 9999f;
        boolean closedOverlay = false;
        boolean waitlistOverlay = false;
        boolean maxTablesVisible = false;
        boolean dangerousConfirm = false;
        boolean liveTable = false;
        int screenW = root.getWidth();
        int screenH = root.getHeight();
        if (screenW <= 0 || screenH <= 0) {
            Activity activity = CURRENT_ACTIVITY.get();
            if (activity != null) {
                screenW = activity.getResources().getDisplayMetrics().widthPixels;
                screenH = activity.getResources().getDisplayMetrics().heightPixels;
            }
        }
        if (screenW <= 0) screenW = 1080;
        if (screenH <= 0) screenH = 1920;

        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (isDangerousConfirm(node.compact)) dangerousConfirm = true;
            if (isExitTableConfirm(node.compact) && confirmTitle == null) confirmTitle = node;
            if (isConfirmYes(node.compact) && yesBtn == null) yesBtn = node;
            if (isLeaveTableMenu(node.compact) && leaveTable == null) leaveTable = node;
            if (isGoToMyTables(node.compact) && goToTables == null) goToTables = node;
            if (isMaxTables(node.compact)) maxTablesVisible = true;
            if (isLiveActionButton(node, screenH)) liveTable = true;
            if (isClosedChip(node, screenW, screenH) && closedChip == null) closedChip = node;
            if ("peye.table_closed".equals(node.role) && closedChip == null) closedChip = node;
            if ("peye.leave_table".equals(node.role) && leaveTable == null) leaveTable = node;
            if ("peye.confirm_yes".equals(node.role) && yesBtn == null) yesBtn = node;
            if (isClosedOverlay(node, screenW, screenH)) closedOverlay = true;
            if (isWaitlist(node.compact)) waitlistOverlay = true;
            if (isOverflowGlyph(node.text, node.compact) && isTabStrip(node, screenW, screenH)
                    && overflowGlyph == null) {
                overflowGlyph = node;
            }
            float wait = turnTimerSeconds(node, screenW, screenH);
            if (wait > 0f && wait < turnTabSeconds) {
                turnTabSeconds = wait;
                turnTab = node;
            }
        }

        if (now - LAST_DUMP_MS >= 1000L) {
            LAST_DUMP_MS = now;
            sendUiDump(nodes, screenW, screenH, closedOverlay, waitlistOverlay,
                    turnTabSeconds < 9999f ? turnTabSeconds : -1f, liveTable);
        }

        // Manual mode: never lobby-join, never tab-switch, never waitlist
        // hamburger. Leave only when the trainer armed LEAVE_STICKY.
        boolean leaveJob = LEAVE_STICKY || leaveArmed(1, now);
        boolean leaveConfirm = confirmTitle != null && !dangerousConfirm
            && (LEAVE_STICKY || leaveArmed(2, now));

        if (leaveConfirm) {
            // Understood / Leave & Exit / Yes — the label itself, not a
            // parent overlay whose center misses the button.
            UiNode btn = yesBtn != null ? yesBtn : rightConfirmNode(nodes, screenW, screenH);
            if (btn != null && btn.bounds != null) {
                tapScreen(btn.bounds.centerX(), btn.bounds.centerY(), "confirm-exit-table");
                clearLeave("confirmed");
                LEAVE_STICKY = false;
                showBanner("LEAVE", true, "red");
            }
            return;
        }

        // Closed sibling first. A live ring (pot / 3+ names / action row)
        // must not be hamburged because another tab is leaving.
        if (closedChip != null && !closedOverlay && LEAVE_STICKY) {
            tapNodeCenter(closedChip, "focus-closed-tab");
            return;
        }

        boolean liveRing = liveTable || hasPot(nodes) || feltNames(nodes, screenH) >= 3;
        boolean leaveThisFelt = LEAVE_STICKY && !liveRing;
        if (leaveThisFelt) {
            if (leaveTable != null) {
                if (tapNodeCenter(leaveTable, "leave-table")) {
                    armLeave(2, now);
                }
                return;
            }
            if (overflowGlyph != null && !leaveArmed(1, now)) {
                if (tapView(clickableOf(overflowGlyph.view), "overflow-glyph")) {
                    armLeave(1, now);
                }
                return;
            }
            View burger = pickHamburger(nodes, screenW, screenH);
            if (burger != null && !leaveArmed(1, now)) {
                tapView(burger, "leave-hamburger");
                armLeave(1, now);
                return;
            }
            if (closedChip != null && !closedOverlay && !leaveArmed(1, now)) {
                tapView(clickableOf(closedChip.view), "focus-closed-tab");
                return;
            }
        }

        JOIN_ARMED = false;
    }

    private static final class UiNode {
        final View view;
        final String text;
        final String compact;
        final String role;
        final Rect bounds;
        UiNode(View view, String text, String compact, String role, Rect bounds) {
            this.view = view;
            this.text = text;
            this.compact = compact;
            this.role = role == null ? "" : role;
            this.bounds = bounds;
        }
    }

    private static final class HudLayer extends View {
        private final Paint bannerBg = new Paint();
        private final Paint bannerFg = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint amountFg = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint delayFg = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint xPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private String banner = "";
        private String bannerAction = "";
        private String bannerAmount = "";
        private String bannerDelay = "";
        private String bannerTone = "";
        private boolean bannerFromCc = true;
        private boolean bannerSticky = false;
        private long bannerUntil = 0L;
        private int tapX = -1;
        private int tapY = -1;
        private long tapUntil = 0L;
        private String tapWhy = "";

        HudLayer(Context context) {
            super(context);
            setWillNotDraw(false);
            bannerBg.setColor(0xE6111318);
            bannerFg.setColor(Color.WHITE);
            bannerFg.setTextSize(26f);
            bannerFg.setFakeBoldText(true);
            amountFg.setColor(Color.WHITE);
            amountFg.setTextSize(26f);
            amountFg.setFakeBoldText(true);
            delayFg.setColor(0x88FFFFFF);
            delayFg.setTextSize(16f);
            delayFg.setFakeBoldText(false);
            xPaint.setColor(0xFFFF3B30);
            xPaint.setStrokeWidth(8f);
            xPaint.setStyle(Paint.Style.STROKE);
        }

        void setBanner(String text, boolean sticky, String tone, boolean fromCc) {
            // One bar, latest message only. Leave and hints share this slot.
            banner = text == null ? "" : text.trim();
            bannerAction = "";
            bannerAmount = "";
            bannerDelay = "";
            bannerFromCc = fromCc;
            if (banner.length() > 0) {
                String[] parts = banner.split(" +");
                if (parts.length >= 3 && isHintAction(parts[0])
                        && isHintNumber(parts[1]) && isHintInt(parts[2])) {
                    bannerAction = parts[0];
                    bannerAmount = parts[1];
                    bannerDelay = parts[2];
                } else if (parts.length == 1 && isHintAction(parts[0])) {
                    bannerAction = parts[0];
                }
            }
            bannerTone = tone == null ? "" : tone.trim().toLowerCase();
            bannerSticky = sticky;
            if (banner.length() == 0) {
                bannerUntil = 0L;
                bannerSticky = false;
            } else {
                // Sticky only lengthens TTL. Never keep the bar after execution.
                long life = sticky ? 8000L : 2500L;
                bannerUntil = SystemClock.uptimeMillis() + life;
            }
            postInvalidate();
            postDelayed(new Runnable() {
                @Override public void run() { invalidate(); }
            }, life + 50L);
        }

        void setTap(int x, int y, String why) {
            tapX = x;
            tapY = y;
            tapWhy = why == null ? "" : why;
            tapUntil = SystemClock.uptimeMillis() + 800L;
            postInvalidate();
            postDelayed(new Runnable() {
                @Override public void run() { invalidate(); }
            }, 850);
        }

        @Override
        protected void onDraw(Canvas canvas) {
            long now = SystemClock.uptimeMillis();
            if (banner.length() > 0 && now < bannerUntil) {
                int w = getWidth();
                float pad = 10f;
                float th = bannerFg.getTextSize();
                int bg = toneColor(bannerTone, banner, bannerAction);
                bannerBg.setColor(bg);
                int fg = contrastOn(bg);
                bannerFg.setColor(fg);
                amountFg.setColor(fg);
                boolean delayWarn = bannerDelay.equals("0") && !bannerFromCc;
                delayFg.setColor(delayWarn ? 0xFFFF6B73 : (fg & 0x00FFFFFF) | 0x88000000);
                canvas.drawRect(0, 0, w, th + pad * 2f + 8f, bannerBg);
                if (bannerAction.length() > 0) {
                    canvas.drawText(bannerAction, pad, pad + th, bannerFg);
                    float mid = w / 2f - amountFg.measureText(bannerAmount) / 2f;
                    canvas.drawText(bannerAmount, mid, pad + th, amountFg);
                    float right = w - pad - delayFg.measureText(bannerDelay);
                    canvas.drawText(bannerDelay, right, pad + th, delayFg);
                } else {
                    canvas.drawText(banner, pad, pad + th, bannerFg);
                }
            }
            if (now < tapUntil && tapX >= 0) {
                int[] loc = new int[2];
                getLocationOnScreen(loc);
                float x = tapX - loc[0];
                float y = tapY - loc[1];
                canvas.drawLine(x - 36, y - 36, x + 36, y + 36, xPaint);
                canvas.drawLine(x + 36, y - 36, x - 36, y + 36, xPaint);
                canvas.drawCircle(x, y, 28, xPaint);
                if (tapWhy.length() > 0) {
                    canvas.drawText(tapWhy, x + 40, y - 20, bannerFg);
                }
            }
        }
    }

    // Official RN slots. Coin does not set testID on lobby/table chrome;
    // we stamp peye.* here so later frames tap by id, not by locale text.
    private static final int RN_TEST_ID = 0x7f0a02cd;
    private static final int RN_NATIVE_ID = 0x7f0a03b9;

    private static String taggedRole(View view) {
        if (view == null) return "";
        Object a = view.getTag(RN_TEST_ID);
        if (a instanceof String) {
            String s = ((String) a).trim();
            if (s.startsWith("peye.")) return s;
        }
        Object b = view.getTag(RN_NATIVE_ID);
        if (b instanceof String) {
            String s = ((String) b).trim();
            if (s.startsWith("peye.")) return s;
        }
        return "";
    }

    private static void stampRole(View view, String role) {
        if (view == null || role == null || role.length() == 0) return;
        view.setTag(RN_TEST_ID, role);
        view.setTag(RN_NATIVE_ID, role);
        View click = clickableOf(view);
        if (click != null && click != view) {
            click.setTag(RN_TEST_ID, role);
            click.setTag(RN_NATIVE_ID, role);
        }
    }

    private static String inferRole(String compact) {
        if (compact == null || compact.length() == 0) return "";
        if (compact.contains("joinsimilar") || compact.equals("similar")) return "peye.join_similar";
        if ((compact.equals("cash") || compact.equals("cashgames") || compact.contains("cashgame"))
                && compact.indexOf("free") < 0) {
            return "peye.cash_games";
        }
        if (compact.contains("multiway")) return "peye.multiway";
        if (compact.contains("headsup") || compact.equals("nlhhu")) return "peye.heads_up";
        if (isLeaveTableMenu(compact)) return "peye.leave_table";
        if (isConfirmYes(compact)) return "peye.confirm_yes";
        if (isTableClosed(compact)) return "peye.table_closed";
        if (compact.equals("fold") || compact.equals("\u0444\u043e\u043b\u0434")) return "peye.action_fold";
        if (compact.equals("check") || compact.equals("\u0447\u0435\u043a")) return "peye.action_check";
        if (compact.equals("call") || compact.equals("\u043a\u043e\u043b\u043b")) return "peye.action_call";
        if (compact.equals("raise") || compact.equals("\u0440\u0435\u0439\u0437")) return "peye.action_raise";
        return "";
    }

    private static UiNode findByRole(List<UiNode> nodes, String role) {
        if (role == null || role.length() == 0) return null;
        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (role.equals(node.role)) return node;
        }
        return null;
    }

    private static void collectUiNodes(View view, List<UiNode> out) {
        if (view == null || !view.isShown()) return;
        String text = viewLabel(view);
        if (text.length() > 0 || view.isClickable()) {
            Rect bounds = new Rect();
            if (view.getGlobalVisibleRect(bounds) && !bounds.isEmpty()) {
                String c = compact(text);
                String role = taggedRole(view);
                if (role.length() == 0) role = taggedRole(clickableOf(view));
                if (role.length() == 0) role = inferRole(c);
                if (role.length() > 0) stampRole(view, role);
                out.add(new UiNode(view, text, c, role, bounds));
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

    private static boolean isWaitlist(String c) {
        return c.contains("joinwaitlist")
                || c.contains("findmeaseat")
                || c.contains("playersinline")
                || c.contains("\u043d\u0430\u0439\u0442\u0438\u043c\u043d\u0435\u043c\u0435\u0441\u0442\u043e")
                || c.contains("\u0432\u0441\u0442\u0430\u0442\u044c\u0432\u043e\u0447\u0435\u0440\u0435\u0434\u044c");
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

    private static boolean isCashGames(String c) {
        if (c == null) return false;
        if (c.equals("freegames") || c.equals("freegame")) return false;
        return c.equals("cash") || c.equals("cashgames") || c.equals("ring")
            || c.equals("ringgames") || c.equals("playcash") || c.equals("playcashgames")
            || c.contains("cashgame") || c.contains("\u043a\u044d\u0448");
    }

    private static boolean isHoldemLabel(String c) {
        if (c == null || isHuLabel(c) || isMultiwayLabel(c)) return false;
        String n = stripChipMark(c);
        return n.equals("nlh") || n.equals("holdem") || n.equals("nlhe")
            || n.equals("texas") || n.equals("hold'em")
            || n.contains("\u0445\u043e\u043b\u0434\u0435\u043c");
    }

    private static boolean isPloLabel(String c) {
        if (c == null) return false;
        String n = stripChipMark(c);
        return n.equals("plo") || n.equals("omaha") || n.equals("plo4")
            || n.startsWith("plo") || n.contains("\u043e\u043c\u0430\u0445\u0430");
    }

    private static boolean isChipOn(String c) {
        if (c == null) return false;
        return c.indexOf('\u00d7') >= 0 || c.indexOf('\u2715') >= 0
            || c.indexOf('\u2716') >= 0;
    }

    private static String stripChipMark(String c) {
        if (c == null) return "";
        String n = c.replace("\u00d7", "").replace("\u2715", "").replace("\u2716", "");
        if (n.endsWith("x") && n.length() > 1) n = n.substring(0, n.length() - 1);
        return n;
    }

    private static boolean isHuLabel(String c) {
        if (c == null || c.equals("nlh") || c.equals("holdem") || isMultiwayLabel(c)) return false;
        return c.equals("hu") || c.equals("nlhhu") || c.equals("headsup")
            || c.equals("heads-up") || c.contains("headsup")
            || (c.endsWith("hu") && c.indexOf("6max") < 0);
    }

    private static boolean isSixMaxLabel(String c) {
        if (c == null) return false;
        String n = c.replace("-", "");
        return n.equals("nlh6max") || n.equals("6max") || n.equals("sixmax")
            || n.indexOf("6max") >= 0;
    }

    private static boolean isMultiwayLabel(String c) {
        if (c == null) return false;
        String n = c.replace("-", "");
        return n.contains("multiway") || n.equals("mw")
            || n.contains("\u043c\u0443\u043b\u044c\u0442\u0438\u0432\u0435\u0439");
    }

    private static boolean isJoinSimilar(String c) {
        return c != null && (c.contains("joinsimilar") || c.equals("similar"));
    }

    private static boolean isHomeMarker(String c) {
        return c != null && (c.equals("letsexplore") || c.contains("letsexplore")
            || c.equals("pokerformats") || c.equals("popular") || c.equals("freegames"));
    }

    private static boolean isLoadingLabel(String c) {
        if (c == null || c.length() == 0) return false;
        return c.contains("initializ") || c.contains("loading") || c.contains("connecting")
            || c.contains("pleasewait") || c.contains("tableloading")
            || c.contains("\u0437\u0430\u0433\u0440\u0443\u0437\u043a")
            || c.contains("\u043f\u043e\u0434\u043e\u0436\u0434\u0438\u0442");
    }

    private static boolean isTableTitle(String c) {
        if (c == null) return false;
        return c.startsWith("nlh-") || c.startsWith("plo-")
            || c.startsWith("plo4-") || c.startsWith("plo5-") || c.startsWith("plo6-");
    }

    private static boolean isFeltStakes(String compact, String text, double bb) {
        if (!isLimitLabel(text, bb) || compact == null) return false;
        String c = compact.replace("-", "");
        return c.startsWith("nlh") || c.startsWith("plo") || c.startsWith("omaha");
    }

    private static boolean isStackLabel(String c) {
        if (c == null || !c.endsWith("bb") || c.length() < 3) return false;
        for (int i = 0; i < c.length() - 2; i++) {
            if (c.charAt(i) >= '0' && c.charAt(i) <= '9') return true;
        }
        return false;
    }

    private static boolean isPlayLabel(String c) {
        return c.equals("play") || c.equals("join") || c.equals("seat")
            || c.equals("\u0438\u0433\u0440\u0430\u0442\u044c")
            || c.equals("\u0441\u0435\u0441\u0442\u044c")
            || c.equals("\u043e\u0442\u043a\u0440\u044b\u0442\u044c");
    }

    private static String stripZeros(double value) {
        String s = String.valueOf(value);
        if (s.indexOf('.') >= 0) {
            while (s.endsWith("0")) s = s.substring(0, s.length() - 1);
            if (s.endsWith(".")) s = s.substring(0, s.length() - 1);
        }
        return s;
    }

    private static String stripMoney(String text) {
        if (text == null) return "";
        StringBuilder sb = new StringBuilder(text.length());
        for (int i = 0; i < text.length(); i++) {
            char ch = text.charAt(i);
            if (ch <= 32) continue;
            if (ch == '\u20ae' || ch == '$' || ch == '\u20ac' || ch == '\u00a3'
                    || ch == '\u20b9' || ch == '\u20bd' || ch == '\u00a5'
                    || ch == '\u20a9' || ch == '\u00a4' || ch == '\u20b8') continue;
            if (ch == ',') sb.append('.');
            else sb.append(Character.toLowerCase(ch));
        }
        return sb.toString();
    }

    private static boolean isLimitLabel(String text, double bb) {
        if (text == null) return false;
        String t = stripMoney(text);
        String bbS = stripZeros(bb);
        String sbS = stripZeros(bb / 2.0);
        String pair = sbS + "/" + bbS;
        return t.equals(pair) || t.contains(pair);
    }

    private static boolean parseOcc(String text, int[] out) {
        if (text == null) return false;
        int slash = text.indexOf('/');
        if (slash <= 0 || slash >= text.length() - 1) return false;
        try {
            int taken = Integer.parseInt(text.substring(0, slash).trim());
            int size = Integer.parseInt(text.substring(slash + 1).trim());
            if (size < 2 || size > 9 || taken < 0 || taken > size) return false;
            out[0] = taken;
            out[1] = size;
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static void sweepLobbyJoin(List<UiNode> nodes, int screenW, int screenH) {
        long now = SystemClock.uptimeMillis();
        if (now - LAST_LOBBY_TAP_MS < 1800L) return;
        UiNode six = null;
        UiNode huChip = null;
        UiNode multiway = null;
        UiNode similar = null;
        UiNode occ = null;
        int occScore = 999;
        UiNode limit = null;
        UiNode holdem = null;
        UiNode ploOff = null;
        UiNode cash = null;
        UiNode home = null;
        int empty = 0;
        int ringRows = 0;
        int huRows = 0;
        boolean tableTitle = false;
        boolean homeScreen = false;
        boolean loading = false;
        boolean nlhOn = false;
        boolean ploOn = false;
        int[] pair = new int[2];
        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (node.bounds == null) continue;
            int y = node.bounds.centerY();
            String c = node.compact;
            if (isLoadingLabel(c)) loading = true;
            if (c.equals("empty")) empty++;
            if (isJoinSimilar(c) && similar == null) similar = node;
            if (isTableTitle(c) || isFeltStakes(c, node.text, JOIN_BB)) tableTitle = true;
            if (isHomeMarker(c)) homeScreen = true;
            if (isHuLabel(c) && huChip == null) huChip = node;
            if (isSixMaxLabel(c) && six == null) six = node;
            if (isMultiwayLabel(c) && multiway == null) multiway = node;
            if (isChipOn(c) && isHoldemLabel(c)) nlhOn = true;
            if (isChipOn(c) && isPloLabel(c)) ploOn = true;
            if (isPloLabel(c) && !isChipOn(c) && c.equals("plo")
                    && node.bounds.height() < screenH * 10 / 100 && ploOff == null) {
                ploOff = node;
            }
            if (parseOcc(node.text, pair)) {
                if (pair[1] <= 2) huRows++;
                if (pair[1] >= 6 && pair[0] < pair[1] && pair[0] >= 2) {
                    ringRows++;
                    int score = Math.abs(pair[0] - 4);
                    if (score < occScore) {
                        occScore = score;
                        occ = node;
                    }
                }
            }
            if (isLimitLabel(node.text, JOIN_BB) && node.bounds.height() < screenH * 16 / 100
                    && node.bounds.top > screenH * 8 / 100
                    && !isHuLabel(c) && !isJoinSimilar(c) && !isTableTitle(c)
                    && !isFeltStakes(c, node.text, JOIN_BB) && limit == null) {
                limit = node;
            }
            if (isHoldemLabel(c) && y > screenH * 10 / 100 && holdem == null) holdem = node;
            if (isCashGames(c) && cash == null) cash = node;
            if ((c.equals("lobby") || c.equals("\u043b\u043e\u0431\u0431\u0438") || c.equals("games"))
                    && y > screenH * 70 / 100 && home == null) {
                home = node;
            }
        }
        if (findByRole(nodes, "peye.join_similar") != null) similar = findByRole(nodes, "peye.join_similar");
        if (findByRole(nodes, "peye.multiway") != null) multiway = findByRole(nodes, "peye.multiway");
        if (findByRole(nodes, "peye.cash_games") != null) cash = findByRole(nodes, "peye.cash_games");
        if (loading) return;
        boolean tableChrome = similar != null || tableTitle;
        boolean huFilter = huRows > 0 && ringRows == 0 && multiway == null;
        boolean atFelt = tableChrome || (empty >= 2 && ringRows == 0 && multiway == null
                && cash == null && !homeScreen);
        if (atFelt) {
            JOIN_ARMED = false;
            // Trainer owns leave. Auto-LEAVE here hamburged 4-handed 6-max
            // rings that still showed two Empty seats.
            return;
        }
        if (tableChrome) {
            return;
        }
        UiNode target = null;
        String why = "lobby-join";
        if (occ != null) { target = occ; why = "lobby-table"; }
        else if (multiway != null) { target = multiway; why = "lobby-multiway"; }
        else if (huFilter) {
            if (six != null) { target = six; why = "lobby-6max"; }
            else if (huChip != null) { target = huChip; why = "lobby-clear-hu"; }
        } else if (nlhOn && !ploOn && ploOff != null) { target = ploOff; why = "lobby-plo"; }
        else if (limit != null) { target = limit; why = "lobby-limit"; }
        else if (cash != null && homeScreen) { target = cash; why = "lobby-cash"; }
        else if (home != null) { target = home; why = "lobby-home"; }
        if (target == null) return;
        LAST_LOBBY_TAP_MS = now;
        tapScreen(target.bounds.centerX(), target.bounds.centerY(), why);
    }

    private static boolean isActionLabel(String c) {
        return c.equals("fold") || c.equals("check") || c.equals("call")
                || c.equals("raise") || c.equals("bet") || c.equals("allin")
                || c.equals("all-in") || c.equals("check/fold") || c.equals("checkfold")
                || c.equals("\u0444\u043e\u043b\u0434") || c.equals("\u0447\u0435\u043a")
                || c.equals("\u043a\u043e\u043b\u043b") || c.equals("\u0440\u0435\u0439\u0437")
                || c.equals("\u0431\u0435\u0442");
    }

    private static boolean isLiveActionButton(UiNode node, int screenH) {
        if (node == null || node.bounds == null) return false;
        // Bottom action row only. Seat captions ("Fold") sit higher and used
        // to freeze tab switching plus steal the hamburger.
        if (node.bounds.centerY() < screenH * 68 / 100) return false;
        if (node.bounds.height() < screenH * 4 / 100) return false;
        if (node.bounds.width() < 80) return false;
        View click = clickableOf(node.view);
        if (click == null || !click.isClickable()) return false;
        return isActionLabel(node.compact);
    }

    private static boolean isClosedChip(UiNode node, int screenW, int screenH) {
        if (node == null || node.bounds == null || !isTableClosed(node.compact)) return false;
        return node.bounds.centerY() <= screenH * 22 / 100
                && node.bounds.height() < screenH * 18 / 100
                && node.bounds.width() < screenW * 42 / 100;
    }

    private static boolean isClosedOverlay(UiNode node, int screenW, int screenH) {
        if (node == null || node.bounds == null || !isTableClosed(node.compact)) return false;
        return !isClosedChip(node, screenW, screenH);
    }

    private static float turnTimerSeconds(UiNode node, int screenW, int screenH) {
        if (node == null || node.bounds == null) return -1f;
        if (node.bounds.centerY() > screenH * 24 / 100) return -1f;
        if (node.bounds.height() > screenH * 16 / 100) return -1f;
        if (isTableClosed(node.compact) || isOverflowGlyph(node.text, node.compact)) return -1f;
        String t = node.text == null ? "" : node.text.trim().toLowerCase().replace(" ", "");
        String num = t;
        if (t.endsWith("s") && t.length() >= 2 && t.length() <= 6) {
            num = t.substring(0, t.length() - 1).replace(',', '.');
        } else if (!isAllDigits(t) || t.length() < 1 || t.length() > 3) {
            return -1f;
        }
        try {
            float value = Float.parseFloat(num.replace(',', '.'));
            if (value > 0f && value <= 120f) return value;
        } catch (Throwable ignored) {}
        return -1f;
    }

    private static boolean isTurnTimerTab(UiNode node, int screenW, int screenH) {
        return turnTimerSeconds(node, screenW, screenH) > 0f;
    }

    private static boolean isAllDigits(String s) {
        if (s == null || s.length() == 0) return false;
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch < '0' || ch > '9') return false;
        }
        return true;
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
        if (c == null || c.length() == 0) return false;
        return c.equals("yes") || c.equals("\u0434\u0430") || c.equals("ok")
                || c.equals("okay") || c.equals("confirm") || c.equals("\u043e\u043a")
                || c.equals("understood") || c.equals("\u043f\u043e\u043d\u044f\u0442\u043d\u043e")
                || c.equals("\u043f\u043e\u043d\u044f\u043b")
                || c.equals("leaveandexit") || c.equals("leaveandexittable")
                || c.equals("leaveexit");
    }

    private static boolean isOverflowGlyph(String text, String compact) {
        // Table-tab ellipsis only. "menu" / vertical ellipsis is the app
        // hamburger; clicking it every 700ms is why the menu never stayed open.
        String trimmed = text == null ? "" : text.trim();
        return trimmed.equals("...") || trimmed.equals("\u2026")
                || trimmed.equals("\u2022\u2022\u2022")
                || trimmed.equals("\u22ef");
    }

    private static boolean isHamburgerZone(UiNode node, int screenW, int screenH) {
        if (node == null || node.bounds == null) return false;
        return node.bounds.centerX() <= screenW * 14 / 100
                && node.bounds.centerY() <= screenH * 18 / 100;
    }

    private static boolean isTabStrip(UiNode node, int screenW, int screenH) {
        if (node == null || node.bounds == null) return false;
        int cx = node.bounds.centerX();
        int cy = node.bounds.centerY();
        return cy <= screenH * 22 / 100
                && cx > screenW * 14 / 100
                && cx < screenW * 88 / 100;
    }

    private static boolean isTopLeft(UiNode node, int screenW, int screenH) {
        return isHamburgerZone(node, screenW, screenH);
    }

    private static View clickableOf(View view) {
        View cur = view;
        while (cur != null && !cur.isClickable()) {
            Object parent = cur.getParent();
            cur = parent instanceof View ? (View) parent : null;
        }
        return cur != null && cur.isClickable() && cur.isShown() ? cur : view;
    }

    private static boolean tapNodeCenter(UiNode node, String why) {
        if (node == null || node.bounds == null) return false;
        return tapScreen(node.bounds.centerX(), node.bounds.centerY(), why);
    }

    private static boolean hasPot(List<UiNode> nodes) {
        for (int i = 0; i < nodes.size(); i++) {
            String c = nodes.get(i).compact;
            if (c.equals("pot") || c.startsWith("pot")) return true;
        }
        return false;
    }

    private static int feltNames(List<UiNode> nodes, int screenH) {
        int n = 0;
        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (node.bounds == null) continue;
            int y = node.bounds.centerY();
            if (y < screenH * 22 / 100 || y > screenH * 80 / 100) continue;
            String c = node.compact;
            if (c.length() == 0 || c.equals("empty") || isActionLabel(c)
                    || c.equals("pot") || c.startsWith("pot") || c.equals("nlh")
                    || c.equals("plo") || c.contains("joinsimilar")) continue;
            if (isAllDigits(c)) continue;
            n++;
        }
        return n;
    }

    private static UiNode rightConfirmNode(List<UiNode> nodes, int screenW, int screenH) {
        UiNode best = null;
        int bestX = -1;
        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (node.bounds == null) continue;
            if (node.bounds.centerY() < screenH * 45 / 100) continue;
            if (node.bounds.height() > screenH * 20 / 100) continue;
            String c = node.compact;
            if (c.equals("no") || c.equals("\u043d\u0435\u0442") || c.equals("cancel")
                    || c.equals("\u043e\u0442\u043c\u0435\u043d\u0430")
                    || c.contains("\u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c")) continue;
            int x = node.bounds.centerX();
            if (x > bestX && x > screenW * 45 / 100) {
                bestX = x;
                best = node;
            }
        }
        return best;
    }

    private static View rightConfirmButton(List<UiNode> nodes, int screenW, int screenH) {
        UiNode node = rightConfirmNode(nodes, screenW, screenH);
        return node == null ? null : node.view;
    }

    private static View pickHamburger(List<UiNode> nodes, int screenW, int screenH) {
        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (!isHamburgerZone(node, screenW, screenH)) continue;
            if (isOverflowGlyph(node.text, node.compact)) continue;
            View click = clickableOf(node.view);
            if (click != null && click.isShown()) return click;
        }
        return null;
    }

    private static View pickTabOverflow(List<UiNode> nodes, int screenW, int screenH) {
        List<View> cands = new ArrayList<View>();
        List<Integer> xs = new ArrayList<Integer>();
        for (int i = 0; i < nodes.size(); i++) {
            UiNode node = nodes.get(i);
            if (!isTabStrip(node, screenW, screenH)) continue;
            if (isHamburgerZone(node, screenW, screenH)) continue;
            boolean glyph = isOverflowGlyph(node.text, node.compact);
            if (!glyph) {
                if (node.bounds.width() > screenW * 18 / 100) continue;
                if (node.bounds.height() > screenH * 12 / 100) continue;
                if (node.compact.contains("nlh") || node.compact.contains("plo")
                        || node.compact.contains("join") || node.compact.contains("similar")) continue;
                if (turnTimerSeconds(node, screenW, screenH) > 0f) continue;
            }
            if (node.bounds.width() < 12 || node.bounds.height() < 12) continue;
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

    private static void sendUiDump(
            List<UiNode> nodes, int screenW, int screenH,
            boolean closed, boolean waitlist, float timer, boolean live) {
        try {
            StringBuilder sb = new StringBuilder(Math.min(24000, 96 + nodes.size() * 48));
            sb.append("<ui w=\"").append(screenW).append("\" h=\"").append(screenH)
                .append("\" janitor=\"1\" nodes=\"").append(nodes.size())
                .append("\" closed=\"").append(closed ? 1 : 0)
                .append("\" waitlist=\"").append(waitlist ? 1 : 0)
                .append("\" live=\"").append(live ? 1 : 0)
                .append("\" leave=\"").append(LEAVE_STICKY ? 1 : 0)
                .append("\" timer=\"").append(timer)
                .append("\" tap=\"").append(LAST_TAP_WHY == null ? "" : LAST_TAP_WHY)
                .append("\" tapx=\"").append(LAST_TAP_X)
                .append("\" tapy=\"").append(LAST_TAP_Y)
                .append("\" taphit=\"").append(LAST_TAP_HIT ? 1 : 0)
                .append("\" tapage=\"").append(
                    LAST_TAP_AT_MS <= 0 ? -1 : (SystemClock.uptimeMillis() - LAST_TAP_AT_MS)
                )
                .append("\">");
            int n = Math.min(nodes.size(), 180);
            for (int i = 0; i < n; i++) {
                UiNode node = nodes.get(i);
                if (node.bounds == null) continue;
                String t = node.text == null ? "" : node.text;
                if (t.length() > 48) t = t.substring(0, 48);
                t = t.replace('&', ' ').replace('<', ' ').replace('>', ' ').replace('"', ' ');
                sb.append("<n t=\"").append(t)
                    .append("\" x=\"").append(node.bounds.left)
                    .append("\" y=\"").append(node.bounds.top)
                    .append("\" w=\"").append(node.bounds.width())
                    .append("\" h=\"").append(node.bounds.height())
                    .append("\" c=\"").append(node.view != null && node.view.isClickable() ? 1 : 0)
                    .append("\" id=\"").append(node.role == null ? "" : node.role)
                    .append("\"/>");
            }
            sb.append("</ui>");
            nativeUiDump(sb.toString());
        } catch (Throwable ignored) {}
    }

    private static View reactRootOf(View start) {
        View cur = start;
        View found = null;
        while (cur != null) {
            String name = cur.getClass().getName();
            if (name.indexOf("ReactRootView") >= 0) return cur;
            if (name.indexOf("ReactViewGroup") >= 0) found = cur;
            Object parent = cur.getParent();
            cur = parent instanceof View ? (View) parent : null;
        }
        return found;
    }

    private static boolean dispatchLocal(
            View view, float screenX, float screenY, long downTime, int action) {
        if (view == null || !view.isShown()) return false;
        int[] loc = new int[2];
        view.getLocationOnScreen(loc);
        float x = screenX - loc[0];
        float y = screenY - loc[1];
        MotionEvent ev = MotionEvent.obtain(
            downTime, SystemClock.uptimeMillis(), action, x, y, 0);
        try {
            ev.setSource(InputDevice.SOURCE_TOUCHSCREEN);
            return view.dispatchTouchEvent(ev);
        } catch (Throwable ignored) {
            return false;
        } finally {
            ev.recycle();
        }
    }

    private static void injectScreenTap(final float screenX, final float screenY) {
        Thread worker = new Thread(new Runnable() {
            @Override public void run() {
                try {
                    Instrumentation inst = new Instrumentation();
                    long t0 = SystemClock.uptimeMillis();
                    MotionEvent down = MotionEvent.obtain(
                        t0, t0, MotionEvent.ACTION_DOWN, screenX, screenY, 0);
                    down.setSource(InputDevice.SOURCE_TOUCHSCREEN);
                    inst.sendPointerSync(down);
                    down.recycle();
                    MotionEvent up = MotionEvent.obtain(
                        t0, t0 + 45L, MotionEvent.ACTION_UP, screenX, screenY, 0);
                    up.setSource(InputDevice.SOURCE_TOUCHSCREEN);
                    inst.sendPointerSync(up);
                    up.recycle();
                } catch (Throwable ignored) {}
            }
        }, "hmuriy-tap");
        worker.start();
    }

    public static void onTrainerHud(final String json) {
        Handler handler = new Handler(Looper.getMainLooper());
        handler.post(new Runnable() {
            @Override public void run() {
                applyTrainerHud(json);
            }
        });
    }

    private static void applyTrainerHud(String json) {
        if (json == null || json.length() == 0) return;
        try {
            JSONObject obj = new JSONObject(json);
            String text = obj.optString("text", "");
            String action = obj.optString("action", "");
            boolean leave = obj.optBoolean("leave", false);
            boolean sticky = obj.optBoolean("sticky", false);
            if (text.length() == 0 && action.length() > 0) {
                String amount = obj.has("amount") ? String.valueOf(obj.opt("amount")) : "0.0";
                text = action + " " + amount + " " + obj.optInt("delay_ms", 0);
            }
            if (obj.has("bb")) JOIN_BB = obj.optDouble("bb", JOIN_BB);
            // Leave always beats join. join:true used to clear LEAVE_STICKY
            // on "wait 1 hand" and hamburger the next idle felt.
            JOIN_ARMED = false;
            if (leave) {
                LEAVE_STICKY = true;
                LEAVE_STEP = 0;
            } else if (!sticky) {
                LEAVE_STICKY = false;
            }
            // Manual: never execute trainer lobby taps.
            if (obj.optBoolean("clear", false) || (text.length() == 0 && action.length() == 0)) {
                showBanner("", false, "", false);
                return;
            }
            if (text.length() > 0) {
                String tone = obj.optString("tone", "");
                boolean fromCc = obj.optBoolean("from_cc", !"prefold".equalsIgnoreCase(obj.optString("source", ""))
                    && !"failsafe".equalsIgnoreCase(obj.optString("source", ""))
                    && !"apk".equalsIgnoreCase(obj.optString("source", "")));
                showBanner(text, sticky || leave, tone, fromCc);
            }
        } catch (Throwable ignored) {}
    }

    private static void ensureHud() {
        if (HUD_LAYER != null) return;
        Activity activity = CURRENT_ACTIVITY.get();
        if (activity == null || activity.isFinishing()) return;
        try {
            View decor = activity.getWindow().getDecorView();
            if (!(decor instanceof ViewGroup)) return;
            HudLayer layer = new HudLayer(activity);
            layer.setClickable(false);
            layer.setFocusable(false);
            ((ViewGroup) decor).addView(
                layer,
                new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    Gravity.TOP));
            HUD_LAYER = layer;
        } catch (Throwable ignored) {}
    }

    private static int toneColor(String tone, String text, String action) {
        String t = tone == null ? "" : tone.trim().toLowerCase();
        if (t.length() == 0) t = inferTone(text, action);
        if ("red".equals(t)) return 0xE6B42334;
        if ("green".equals(t)) return 0xE61B7A3A;
        if ("yellow".equals(t) || "amber".equals(t)) return 0xE6C9A227;
        return 0xE6111318;
    }

    private static String inferTone(String text, String action) {
        if (LEAVE_STICKY) return "red";
        String u = ((action != null && action.length() > 0) ? action : (text == null ? "" : text))
            .trim().toUpperCase();
        String raw = text == null ? "" : text.toLowerCase();
        if (u.startsWith("LEAVE") || raw.contains("junk") || raw.contains("broken")
                || raw.contains("error") || raw.contains("мусор")) {
            return "red";
        }
        if (u.startsWith("PREFOLD") || raw.contains("префолд") || raw.contains("apk")) {
            return "red";
        }
        if (u.startsWith("FOLD") || u.startsWith("CHECK") || u.startsWith("CALL")
                || u.startsWith("RAISE") || u.startsWith("BET") || u.startsWith("ALLIN")
                || u.startsWith("ALL-IN") || raw.contains("за столом")) {
            return "green";
        }
        if (JOIN_ARMED || u.startsWith("JOIN") || raw.contains("wait")
                || raw.contains("lobby") || raw.contains("multiway")
                || raw.contains("кэш") || raw.contains("ui-join")) {
            return "yellow";
        }
        return "green";
    }

    private static void showBanner(String text, boolean sticky) {
        showBanner(text, sticky, "", true);
    }

    private static void showBanner(String text, boolean sticky, String tone) {
        showBanner(text, sticky, tone, true);
    }

    private static void showBanner(String text, boolean sticky, String tone, boolean fromCc) {
        ensureHud();
        HudLayer layer = HUD_LAYER;
        if (layer == null) return;
        layer.setBanner(text == null ? "" : text, sticky, tone == null ? "" : tone, fromCc);
    }

    private static int contrastOn(int bg) {
        int r = (bg >> 16) & 0xFF;
        int g = (bg >> 8) & 0xFF;
        int b = bg & 0xFF;
        double y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0;
        return y < 0.45 ? Color.WHITE : 0xFF111318;
    }

    private static void showTapMark(int x, int y, String why) {
        ensureHud();
        HudLayer layer = HUD_LAYER;
        if (layer == null) return;
        layer.setTap(x, y, why == null ? "" : why);
    }

    private static boolean isHintAction(String s) {
        if (s == null || s.length() == 0) return false;
        String c = s.toUpperCase();
        return c.equals("FOLD") || c.equals("CHECK") || c.equals("CALL")
            || c.equals("RAISE") || c.equals("BET") || c.equals("ALLIN")
            || c.equals("ALL-IN") || c.equals("CHECKFOLD") || c.equals("PREFOLD")
            || c.equals("FALLBACK") || c.equals("LEAVE");
    }

    private static boolean isHintNumber(String s) {
        if (s == null || s.length() == 0) return false;
        boolean dot = false;
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch == '.') {
                if (dot) return false;
                dot = true;
                continue;
            }
            if (ch < '0' || ch > '9') return false;
        }
        return true;
    }

    private static boolean isHintInt(String s) {
        if (s == null || s.length() == 0) return false;
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch < '0' || ch > '9') return false;
        }
        return true;
    }

    private static boolean tapScreen(float screenX, float screenY, String why) {
        JANITOR_LAST_TAP_MS = SystemClock.uptimeMillis();
        LAST_TAP_WHY = why == null ? "" : why;
        LAST_TAP_AT_MS = JANITOR_LAST_TAP_MS;
        LAST_TAP_X = (int) screenX;
        LAST_TAP_Y = (int) screenY;
        boolean hit = false;
        long downTime = SystemClock.uptimeMillis();
        List<View> roots = allWindowRoots();
        for (int i = roots.size() - 1; i >= 0; i--) {
            View root = roots.get(i);
            hit = dispatchLocal(root, screenX, screenY, downTime, MotionEvent.ACTION_DOWN) || hit;
            hit = dispatchLocal(root, screenX, screenY, downTime, MotionEvent.ACTION_UP) || hit;
        }
        injectScreenTap(screenX, screenY);
        LAST_TAP_HIT = hit;
        showTapMark(LAST_TAP_X, LAST_TAP_Y, LAST_TAP_WHY);
        Log.i(TAG, "[+] UI janitor tap " + why + " @" + LAST_TAP_X + "," + LAST_TAP_Y
            + " hit=" + (hit ? 1 : 0));
        return true;
    }

    private static boolean tapView(View target, String why) {
        if (target == null || !target.isShown()) return false;
        JANITOR_LAST_TAP_MS = SystemClock.uptimeMillis();
        View click = clickableOf(target);
        View aim = click != null ? click : target;
        int[] loc = new int[2];
        aim.getLocationOnScreen(loc);
        final float screenX = loc[0] + Math.max(1, aim.getWidth()) / 2f;
        final float screenY = loc[1] + Math.max(1, aim.getHeight()) / 2f;
        LAST_TAP_WHY = why == null ? "" : why;
        LAST_TAP_AT_MS = JANITOR_LAST_TAP_MS;
        LAST_TAP_X = (int) screenX;
        LAST_TAP_Y = (int) screenY;
        boolean hit = false;
        try { hit = aim.performClick() || hit; } catch (Throwable ignored) {}
        long downTime = SystemClock.uptimeMillis();
        hit = dispatchLocal(aim, screenX, screenY, downTime, MotionEvent.ACTION_DOWN) || hit;
        hit = dispatchLocal(aim, screenX, screenY, downTime, MotionEvent.ACTION_UP) || hit;
        View react = reactRootOf(aim);
        if (react != null && react != aim) {
            dispatchLocal(react, screenX, screenY, downTime, MotionEvent.ACTION_DOWN);
            dispatchLocal(react, screenX, screenY, downTime, MotionEvent.ACTION_UP);
        }
        injectScreenTap(screenX, screenY);
        LAST_TAP_HIT = hit;
        showTapMark(LAST_TAP_X, LAST_TAP_Y, LAST_TAP_WHY);
        Log.i(TAG, "[+] UI janitor tap " + why + " @" + LAST_TAP_X + "," + LAST_TAP_Y
            + " hit=" + (hit ? 1 : 0));
        return true;
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
                DEVICE_ID, TRANSPORT_ID, proof, TRAINER_HOST, TRAINER_FALLBACK,
                PORT, DEVICE_LABEL);
            if (ready) {
                Log.i(TAG,
                    "[+] libhmuriy ready device=" + DEVICE_ID
                        + " process=" + PROCESS_NAME
                        + " channel=" + PROCESS_KEY
                        + " label=" + DEVICE_LABEL
                        + " trainer=" + TRAINER_HOST + ":" + PORT
                        + " fallback=" + TRAINER_FALLBACK + ":" + PORT
                        + " handshake_ms=3000"
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
