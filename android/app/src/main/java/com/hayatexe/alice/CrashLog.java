package com.hayatexe.alice;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;

import java.io.PrintWriter;
import java.io.StringWriter;

/**
 * Çökme kaydı: uygulama çökerse (QR okuyucu dahil, aynı süreçte çalışır) hatanın yığını saklanır ve bir sonraki
 * açılışta "Kopyala" düğmesiyle gösterilir. Telefon bilgisayara bağlı değilken çökmenin sebebi başka türlü
 * görülemiyordu ("QR kodu okut deyince her seferinde çöküyor", 7 Kasım).
 */
final class CrashLog {
    private static final String PREFS = "alice_crash";
    private static final String KEY = "last";
    private static boolean installed;

    private CrashLog() {}

    /** Bir kez kur; önceki işleyiciye (Android'in "uygulama durdu" penceresi) yine devreder. */
    static synchronized void install(Context ctx) {
        if (installed) return;
        installed = true;
        Context app = ctx.getApplicationContext();
        Thread.UncaughtExceptionHandler prev = Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
            try {
                StringWriter sw = new StringWriter();
                e.printStackTrace(new PrintWriter(sw));
                String text = "ALICE " + BuildConfigInfo.version(app) + " · Android " + Build.VERSION.RELEASE
                        + " (" + Build.MANUFACTURER + " " + Build.MODEL + ")\n" + sw;
                app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                        .putString(KEY, text.length() > 6000 ? text.substring(0, 6000) : text).commit();
            } catch (Throwable ignored) {
                // kayıt yazılamasa da çökme olağan yoluyla sürsün
            }
            if (prev != null) prev.uncaughtException(t, e);
        });
    }

    /** Önceki çökme varsa göster (bir kez). */
    static void showIfAny(Activity a) {
        SharedPreferences p = a.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String text = p.getString(KEY, null);
        if (text == null) return;
        p.edit().remove(KEY).apply();
        new AlertDialog.Builder(a)
                .setTitle("ALICE son açılışta çöktü")
                .setMessage("Hatanın ayrıntısını kopyalayıp ALICE'e (ya da geliştiriciye) gönderirsen sebebi bulunur.\n\n"
                        + (text.length() > 1200 ? text.substring(0, 1200) + "…" : text))
                .setPositiveButton("Kopyala", (d, w) -> {
                    ClipboardManager cm = (ClipboardManager) a.getSystemService(Context.CLIPBOARD_SERVICE);
                    if (cm != null) cm.setPrimaryClip(ClipData.newPlainText("ALICE çökme", text));
                })
                .setNegativeButton("Kapat", null)
                .show();
    }

    /** Sürüm adı (BuildConfig'e bağımlı olmadan). */
    static final class BuildConfigInfo {
        static String version(Context c) {
            try {
                return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName;
            } catch (Exception e) {
                return "?";
            }
        }
    }
}
