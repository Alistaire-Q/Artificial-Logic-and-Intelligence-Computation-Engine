package com.hayatexe.alice;

import android.content.Context;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** Bilgisayardaki ALICE'e kısa istekler (bildirimdeki "İzin ver" gibi). Ana iş parçacığında çağrılmaz. */
final class Api {
    private Api() {}

    static String get(Context c, String path) throws IOException {
        return call(c, "GET", path, null);
    }

    static String post(Context c, String path, String json) throws IOException {
        return call(c, "POST", path, json);
    }

    private static String call(Context c, String method, String path, String json) throws IOException {
        HttpURLConnection h = (HttpURLConnection) new URL(Prefs.base(c) + path).openConnection();
        h.setConnectTimeout(5000);
        h.setReadTimeout(15000);
        h.setRequestMethod(method);
        h.setRequestProperty("X-Alice-Token", Prefs.token(c));
        if (json != null) {
            h.setDoOutput(true);
            h.setRequestProperty("Content-Type", "application/json");
            try (OutputStream o = h.getOutputStream()) {
                o.write(json.getBytes(StandardCharsets.UTF_8));
            }
        }
        int code = h.getResponseCode();
        InputStream in = code >= 400 ? h.getErrorStream() : h.getInputStream();
        if (in == null) throw new IOException("HTTP " + code);
        try (InputStream is = in) {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) b.write(buf, 0, n);
            return b.toString("UTF-8");
        } finally {
            h.disconnect();
        }
    }
}
