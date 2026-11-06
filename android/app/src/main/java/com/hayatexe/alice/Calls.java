package com.hayatexe.alice;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.Ringtone;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;

/**
 * ALICE'in araması: uygulama açıkken zil (Ringer), kapalıyken gelen arama bildirimi (tam ekran, Aç / Reddet).
 * Görüşmenin kendisi telefon arayüzünde (m.js, ARAMA bölümü).
 */
final class Calls {
    static final String CH_CALL = "call";
    static final int ID_CALL = 4, ID_MISSED = 5;
    private static Ringtone tone;

    private Calls() {}

    /* ---------------------------------------------------------------- zil (uygulama öndeyken) */
    static synchronized void ring(Context c, boolean on) {
        Vibrator v = c.getSystemService(Vibrator.class);
        if (!on) {
            if (tone != null) tone.stop();
            tone = null;
            if (v != null) v.cancel();
            return;
        }
        if (tone != null && tone.isPlaying()) return;
        try {
            Uri u = RingtoneManager.getActualDefaultRingtoneUri(c, RingtoneManager.TYPE_RINGTONE);
            if (u == null) u = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
            tone = RingtoneManager.getRingtone(c, u);
            if (tone != null) {
                tone.setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
                if (Build.VERSION.SDK_INT >= 28) tone.setLooping(true);
                tone.play();
            }
        } catch (Exception ignored) {
            tone = null;
        }
        if (v != null && v.hasVibrator()) v.vibrate(VibrationEffect.createWaveform(new long[]{0, 700, 900}, 0));
    }

    /* ---------------------------------------------------------------- bildirim (uygulama kapalıyken) */
    static void channel(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null || nm.getNotificationChannel(CH_CALL) != null) return;
        NotificationChannel ch = new NotificationChannel(CH_CALL, "Aramalar", NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription("ALICE seni aradığında çalar");
        ch.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE), new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        ch.enableVibration(true);
        ch.setVibrationPattern(new long[]{0, 700, 900, 700, 900});
        ch.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(ch);
    }

    static Intent open(Context c, String id, String action) {
        Intent i = new Intent(c, MainActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        i.putExtra("call_id", id);
        i.putExtra("call_action", action);
        return i;
    }

    static void notifyIncoming(Context c, String id, String reason, String text) {
        channel(c);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        PendingIntent show = PendingIntent.getActivity(c, 41, open(c, id, "show"), flags);
        PendingIntent answer = PendingIntent.getActivity(c, 42, open(c, id, "answer"), flags);
        Intent dec = new Intent(c, ActionReceiver.class).putExtra("call_id", id).putExtra("notif", ID_CALL);
        PendingIntent decline = PendingIntent.getBroadcast(c, 43, dec, flags);
        Notification.Builder b = new Notification.Builder(c, CH_CALL)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("ALICE arıyor")
                .setContentText(reason == null || reason.isEmpty() ? text : reason)
                .setCategory(Notification.CATEGORY_CALL)
                .setOngoing(true)
                .setAutoCancel(true)
                .setTimeoutAfter(45000)
                .setContentIntent(show)
                .setFullScreenIntent(show, true)
                .addAction(new Notification.Action.Builder(null, "Reddet", decline).build())
                .addAction(new Notification.Action.Builder(null, "Aç", answer).build());
        Notification n = b.build();
        n.flags |= Notification.FLAG_INSISTENT;  // açılana kadar zil sürsün
        nm.notify(ID_CALL, n);
    }

    static void cancel(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm != null) nm.cancel(ID_CALL);
    }

    static void missed(Context c, String text) {
        cancel(c);
        EventService.channels(c);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        nm.notify(ID_MISSED, new Notification.Builder(c, EventService.CH_ASK)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("Cevapsız arama: ALICE")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setAutoCancel(true)
                .setContentIntent(EventService.openApp(c, "chat", ID_MISSED))
                .build());
    }
}
