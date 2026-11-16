package com.hayatexe.alice;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import java.net.HttpURLConnection;
import java.net.URL;

/** Bağlantı bilgisi: bilgisayardaki ALICE'in yerel adresi (http://192.168.1.24:8765), varsa evin dışından
 *  ulaşmak için aktarma sunucusunun adresi (https://alice-relay.….workers.dev) ve telefon anahtarı. */
final class Prefs {
    private static final String NAME = "alice";

    private Prefs() {}

    private static SharedPreferences sp(Context c) {
        return c.getSharedPreferences(NAME, Context.MODE_PRIVATE);
    }

    /** Şu an kullanılan adres: evdeyken yerel, dışarıdayken aktarma (pick() seçer). */
    private static volatile String active = null;

    static String lan(Context c) { return sp(c).getString("base", ""); }
    static String relay(Context c) { return sp(c).getString("relay", ""); }
    static String base(Context c) { String a = active; return a != null ? a : lan(c); }
    static boolean viaRelay(Context c) { String r = relay(c); return !r.isEmpty() && r.equals(active); }

    /** Hangi adres: önce bilgisayara doğrudan (aynı ağdaysak hızlı), olmazsa aktarma sunucusu.
     *  Ağ isteği yapar: ana iş parçacığında çağrılmaz. */
    static String pick(Context c) {
        String lan = lan(c), relay = relay(c);
        if (relay.isEmpty() || reachable(lan + "/api/state", token(c), 1500)) active = lan;
        else active = relay;
        return active;
    }

    private static boolean reachable(String url, String tok, int ms) {
        HttpURLConnection h = null;
        try {
            h = (HttpURLConnection) new URL(url).openConnection();
            h.setRequestProperty("X-Alice-Token", tok);  // anahtar adreste değil başlıkta
            h.setConnectTimeout(ms);
            h.setReadTimeout(ms + 1500);
            return h.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        } finally {
            if (h != null) h.disconnect();
        }
    }
    static String token(Context c) { return sp(c).getString("token", ""); }
    static boolean configured(Context c) { return !base(c).isEmpty() && !token(c).isEmpty(); }

    static boolean notifications(Context c) { return sp(c).getBoolean("notify", true); }
    static void setNotifications(Context c, boolean on) { sp(c).edit().putBoolean("notify", on).apply(); }

    static double lastEventTs(Context c) { return Double.longBitsToDouble(sp(c).getLong("last_event", 0)); }
    static void setLastEventTs(Context c, double ts) { sp(c).edit().putLong("last_event", Double.doubleToLongBits(ts)).apply(); }

    /** "alice phone"un gösterdiği adres (http://ip:port/m?t=anahtar[&r=aktarma]) ya da alice://connect?url=... */
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
        String r = u.getQueryParameter("r");
        if (r != null && !r.startsWith("https://")) r = null;  // aktarma yalnızca şifreli bağlantıyla
        if (r != null) while (r.endsWith("/")) r = r.substring(0, r.length() - 1);
        SharedPreferences.Editor e = sp(c).edit().putString("base", base).putString("token", t);
        if (r != null) e.putString("relay", r);
        else if (!base.startsWith("https://")) e.remove("relay");
        e.apply();
        active = null;
        return true;
    }

    static void clear(Context c) { sp(c).edit().remove("base").remove("token").remove("relay").apply(); active = null; }

    static String appUrl(Context c) {
        String r = relay(c);
        return base(c) + "/m?t=" + Uri.encode(token(c)) + (r.isEmpty() ? "" : "&r=" + Uri.encode(r));
    }
}
