package com.hayatexe.alice;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

/** Bağlantı bilgisi: bilgisayardaki ALICE'in adresi (http://192.168.1.24:8765) ve telefon anahtarı. */
final class Prefs {
    private static final String NAME = "alice";

    private Prefs() {}

    private static SharedPreferences sp(Context c) {
        return c.getSharedPreferences(NAME, Context.MODE_PRIVATE);
    }

    static String base(Context c) { return sp(c).getString("base", ""); }
    static String token(Context c) { return sp(c).getString("token", ""); }
    static boolean configured(Context c) { return !base(c).isEmpty() && !token(c).isEmpty(); }

    static boolean notifications(Context c) { return sp(c).getBoolean("notify", true); }
    static void setNotifications(Context c, boolean on) { sp(c).edit().putBoolean("notify", on).apply(); }

    static double lastEventTs(Context c) { return Double.longBitsToDouble(sp(c).getLong("last_event", 0)); }
    static void setLastEventTs(Context c, double ts) { sp(c).edit().putLong("last_event", Double.doubleToLongBits(ts)).apply(); }

    /** "alice phone"un gösterdiği adres (http://ip:port/m?t=anahtar) ya da alice://connect?url=... */
    static boolean saveFromUrl(Context c, String raw) {
        if (raw == null) return false;
        raw = raw.trim();
        Uri u = Uri.parse(raw);
        if ("alice".equals(u.getScheme()) && u.getQueryParameter("url") != null) u = Uri.parse(u.getQueryParameter("url"));
        String scheme = u.getScheme();
        String host = u.getHost();
        String t = u.getQueryParameter("t");
        if (scheme == null || host == null || t == null || t.length() < 16) return false;
        if (!scheme.equals("http") && !scheme.equals("https")) return false;
        String base = scheme + "://" + host + (u.getPort() > 0 ? ":" + u.getPort() : "");
        sp(c).edit().putString("base", base).putString("token", t).apply();
        return true;
    }

    static void clear(Context c) { sp(c).edit().remove("base").remove("token").apply(); }

    static String appUrl(Context c) { return base(c) + "/m?t=" + Uri.encode(token(c)); }
}
