package com.hayatexe.alice;

import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.widget.Toast;

import org.json.JSONObject;

/** Bildirimdeki "İzin ver" / "Reddet": cevabı uygulamayı açmadan bilgisayara yollar. */
public class ActionReceiver extends BroadcastReceiver {
    static PendingIntent pending(Context c, String permId, String answer, int notifId) {
        Intent i = new Intent(c, ActionReceiver.class);
        i.putExtra("id", permId);
        i.putExtra("answer", answer);
        i.putExtra("notif", notifId);
        int req = (permId + answer).hashCode();
        return PendingIntent.getBroadcast(c, req, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /** "ALICE arıyor" bildirimindeki Reddet: aramayı uygulamayı açmadan kapatır. */
    static PendingIntent declineCall(Context c, String callId, int notifId) {
        Intent i = new Intent(c, ActionReceiver.class);
        i.putExtra("callId", callId);
        i.putExtra("notif", notifId);
        return PendingIntent.getBroadcast(c, ("call" + callId).hashCode(), i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    @Override
    public void onReceive(Context c, Intent i) {
        String callId = i.getStringExtra("callId");
        if (callId != null) {
            decline(c, callId, i.getIntExtra("notif", 0));
            return;
        }
        String id = i.getStringExtra("id"), answer = i.getStringExtra("answer");
        int notif = i.getIntExtra("notif", 0);
        PendingResult pr = goAsync();
        new Thread(() -> {
            String msg;
            try {
                JSONObject body = new JSONObject().put("id", id).put("answer", answer);
                JSONObject r = new JSONObject(Api.post(c, "/api/permission", body.toString()));
                msg = r.optBoolean("ok", true) ? ("y".equals(answer) ? "İzin verildi" : "Reddedildi") : r.optString("error", "olmadı");
                NotificationManager nm = c.getSystemService(NotificationManager.class);
                if (nm != null) nm.cancel(notif);
            } catch (Exception e) {
                msg = "Bilgisayara ulaşılamadı";
            }
            final String m = msg;
            new android.os.Handler(android.os.Looper.getMainLooper()).post(() -> Toast.makeText(c, m, Toast.LENGTH_SHORT).show());
            pr.finish();
        }).start();
    }

    private void decline(Context c, String callId, int notif) {
        PendingResult pr = goAsync();
        new Thread(() -> {
            String msg;
            try {
                JSONObject body = new JSONObject().put("id", callId);
                JSONObject r = new JSONObject(Api.post(c, "/api/call/decline", body.toString()));
                msg = r.optBoolean("ok", true) ? "Arama reddedildi" : r.optString("error", "olmadı");
                NotificationManager nm = c.getSystemService(NotificationManager.class);
                if (nm != null) nm.cancel(notif);
            } catch (Exception e) {
                msg = "Bilgisayara ulaşılamadı";
            }
            final String m = msg;
            new android.os.Handler(android.os.Looper.getMainLooper()).post(() -> Toast.makeText(c, m, Toast.LENGTH_SHORT).show());
            pr.finish();
        }).start();
    }
}
