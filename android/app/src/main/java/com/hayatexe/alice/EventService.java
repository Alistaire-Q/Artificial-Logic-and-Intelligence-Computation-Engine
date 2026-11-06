package com.hayatexe.alice;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;

/**
 * Uygulama kapalıyken de ALICE'i dinler ve önemli şeyleri bildirim olarak gösterir:
 *  - ALICE izin istediğinde (komut çalıştırma, mesaj gönderme): bildirimde "İzin ver" ve "Reddet"
 *  - ALICE soru sorduğunda (seçenekli soru, projedeki ajanın sorusu)
 *  - bilgisayardaki önemli uyarılar (GPU ısındı, disk doldu...): çekirdeğin olay kaydından
 * Bilgisayardaki olay akışına (/api/events) bağlı kalır; koparsa artan aralıklarla yeniden dener.
 */
public class EventService extends Service {
    static final String CH_STATUS = "status", CH_ASK = "ask", CH_ALERT = "alert";
    static final int ID_STATUS = 1, ID_ALERT_GROUP = 2;
    private static final String GROUP = "alice.alerts";

    private volatile boolean running;
    private Thread sse, poll;
    private final Set<String> seenQuestions = new HashSet<>();

    static void start(Context c) {
        if (!Prefs.configured(c) || !Prefs.notifications(c)) return;
        if (Build.VERSION.SDK_INT >= 33 && c.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return;
        try {
            c.startForegroundService(new Intent(c, EventService.class));
        } catch (Exception ignored) {
            // Android arka plandan başlatmaya izin vermediyse uygulama açılınca yeniden denenir.
        }
    }

    static void channels(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel st = new NotificationChannel(CH_STATUS, "Bağlantı", NotificationManager.IMPORTANCE_MIN);
        st.setDescription("ALICE'e bağlı olduğunu gösteren sessiz bildirim");
        st.setShowBadge(false);
        NotificationChannel ask = new NotificationChannel(CH_ASK, "Onay ve sorular", NotificationManager.IMPORTANCE_HIGH);
        ask.setDescription("ALICE izin istediğinde ya da soru sorduğunda");
        NotificationChannel al = new NotificationChannel(CH_ALERT, "Uyarılar", NotificationManager.IMPORTANCE_DEFAULT);
        al.setDescription("Bilgisayardaki önemli uyarılar");
        nm.createNotificationChannel(st);
        nm.createNotificationChannel(ask);
        nm.createNotificationChannel(al);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        channels(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = statusNotification("Bağlanıyor…");
        if (Build.VERSION.SDK_INT >= 34) startForeground(ID_STATUS, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(ID_STATUS, n);
        if (!running) {
            running = true;
            sse = new Thread(this::sseLoop, "alice-sse");
            poll = new Thread(this::pollLoop, "alice-poll");
            sse.start();
            poll.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (sse != null) sse.interrupt();
        if (poll != null) poll.interrupt();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    private Notification statusNotification(String text) {
        return new Notification.Builder(this, CH_STATUS)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("ALICE")
                .setContentText(text)
                .setOngoing(true)
                .setShowWhen(false)
                .setContentIntent(openApp(this, null, 0))
                .build();
    }

    private void setStatus(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(ID_STATUS, statusNotification(text));
    }

    static PendingIntent openApp(Context c, String tab, int req) {
        Intent i = new Intent(c, MainActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (tab != null) i.putExtra("tab", tab);
        return PendingIntent.getActivity(c, req, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /** Uyarı ya da soru bildirimi. permId verilirse "İzin ver / Reddet" düğmeleri eklenir. */
    static void notifyAlert(Context c, String title, String body, String tag, String tab, String permId) {
        notifyAlert(c, title, body, tag, tab, permId, false);
    }

    static void notifyAlert(Context c, String title, String body, String tag, String tab, String permId, boolean send) {
        channels(c);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        int id = tag == null ? (int) (System.currentTimeMillis() & 0x7fffffff) : (tag.hashCode() & 0x7fffffff) | 0x100;
        Notification.Builder b = new Notification.Builder(c, permId != null || "bell".equals(tab) ? CH_ASK : CH_ALERT)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setAutoCancel(true)
                .setGroup(GROUP)
                .setContentIntent(openApp(c, tab, id));
        if (permId != null) {
            b.setCategory(Notification.CATEGORY_MESSAGE);
            b.addAction(new Notification.Action.Builder(null, send ? "Gönder" : "İzin ver", ActionReceiver.pending(c, permId, "y", id)).build());
            b.addAction(new Notification.Action.Builder(null, "Reddet", ActionReceiver.pending(c, permId, "n", id)).build());
        }
        nm.notify(id, b.build());
    }

    static void cancel(Context c, String tag) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm != null && tag != null) nm.cancel((tag.hashCode() & 0x7fffffff) | 0x100);
    }

    /** ALICE arıyor: uygulama kapalıyken de çalan, "Aç / Reddet" düğmeli bildirim. */
    static void notifyCall(Context c, String callId, String reason, String text) {
        channels(c);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null || callId == null || callId.isEmpty()) return;
        String tag = "call-" + callId;
        int id = (tag.hashCode() & 0x7fffffff) | 0x100;
        String body = reason.isEmpty() ? text : reason;
        Notification.Builder b = new Notification.Builder(c, CH_ASK)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("ALICE arıyor")
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setCategory(Notification.CATEGORY_CALL)
                .setAutoCancel(true)
                .setOngoing(true)
                .setContentIntent(openApp(c, "chat", id));
        b.addAction(new Notification.Action.Builder(null, "Aç", openApp(c, "chat", id + 1)).build());
        b.addAction(new Notification.Action.Builder(null, "Reddet", ActionReceiver.declineCall(c, callId, id)).build());
        nm.notify(id, b.build());
        setStatus("ALICE arıyor…");
    }

    /* ---------------------------------------------------------------- olay akışı */
    private void sseLoop() {
        long wait = 2000;
        while (running) {
            HttpURLConnection h = null;
            try {
                h = (HttpURLConnection) new URL(Prefs.base(this) + "/api/events?t=" + Prefs.token(this)).openConnection();
                h.setConnectTimeout(6000);
                h.setReadTimeout(60000);  // sunucu boşta 15 sn'de bir canlılık satırı yollar
                h.setRequestProperty("Accept", "text/event-stream");
                BufferedReader r = new BufferedReader(new InputStreamReader(h.getInputStream(), StandardCharsets.UTF_8));
                setStatus("Bağlı · onay ve uyarılar burada görünür");
                wait = 2000;
                String line;
                while (running && (line = r.readLine()) != null) {
                    if (line.startsWith("data:")) handle(line.substring(5).trim());
                }
            } catch (Exception e) {
                if (running) setStatus("Bilgisayara ulaşılamıyor, yeniden deneniyor");
            } finally {
                if (h != null) h.disconnect();
            }
            try { Thread.sleep(wait); } catch (InterruptedException e) { return; }
            wait = Math.min(wait * 2, 60000);
        }
    }

    private void handle(String data) {
        try {
            JSONObject ev = new JSONObject(data);
            String type = ev.optString("type");
            if ("permission".equals(type)) {
                boolean send = "send".equals(ev.optString("risk"));
                String name = ev.optString("name");
                String what = send ? "Şunu göndereceğim:" : "run_command".equals(name) ? "Şu komutu çalıştırmak istiyorum:" : "Şunu yapmak istiyorum:";
                notifyAlert(this, send ? "ALICE bir şey gönderecek" : "ALICE izin istiyor", what + " " + ev.optString("summary"),
                        "perm-" + ev.optString("id"), "bell", ev.optString("id"), send);
            } else if ("ask_choice".equals(type)) {
                notifyAlert(this, "ALICE soruyor", ev.optString("question"), "perm-" + ev.optString("id"), "bell", null);
            } else if ("permission_done".equals(type) || "ask_choice_done".equals(type)) {
                cancel(this, "perm-" + ev.optString("id"));
            } else if ("call".equals(type)) {
                notifyCall(this, ev.optString("id"), ev.optString("reason"), ev.optString("text"));
            } else if ("call_state".equals(type)) {
                if (!"ringing".equals(ev.optString("state"))) cancel(this, "call-" + ev.optString("id"));
            } else if ("call_end".equals(type)) {
                cancel(this, "call-" + ev.optString("id"));
                setStatus("Bağlı · onay ve uyarılar burada görünür");
            }
        } catch (Exception ignored) {
        }
    }

    /* ---------------------------------------------------------------- uyarılar ve proje soruları */
    private void pollLoop() {
        boolean first = Prefs.lastEventTs(this) == 0;
        while (running) {
            try {
                JSONObject st = new JSONObject(Api.get(this, "/api/remote/status"));
                JSONArray evs = st.optJSONArray("events");
                double last = Prefs.lastEventTs(this), newest = last;
                if (evs != null) {
                    for (int i = evs.length() - 1; i >= 0; i--) {
                        JSONObject e = evs.getJSONObject(i);
                        double ts = e.optDouble("ts", 0);
                        if (ts <= last) continue;
                        newest = Math.max(newest, ts);
                        String sev = e.optString("severity");
                        // İlk açılışta eski olaylar bildirilmez; sonra sadece uyarı ve kritikler.
                        if (!first && ("warn".equals(sev) || "critical".equals(sev))) {
                            notifyAlert(this, e.optString("title"), e.optString("body"), "ev-" + ts, "bell", null);
                        }
                    }
                }
                if (newest > last) Prefs.setLastEventTs(this, newest);
                JSONArray projects = new JSONArray(Api.get(this, "/api/projects"));
                for (int i = 0; i < projects.length(); i++) {
                    JSONObject p = projects.getJSONObject(i);
                    if (p.optInt("questions") <= 0) continue;
                    String key = p.optString("id") + ":" + p.optInt("questions");
                    if (seenQuestions.add(key) && !first) {
                        notifyAlert(this, "Projede bir soru var", p.optString("title"), "proj-" + p.optString("id"), "bell", null);
                    }
                }
                first = false;
                Updater.check(this, false);  // yeni sürüm bilgisayara indiyse kendini güncelle (3 saatte bir bakar)
            } catch (Exception ignored) {
                // bilgisayar uyuyor ya da ağ yok: sonraki turda yeniden
            }
            try { Thread.sleep(60000); } catch (InterruptedException e) { return; }
        }
    }
}
