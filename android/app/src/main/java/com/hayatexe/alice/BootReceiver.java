package com.hayatexe.alice;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Telefon açılınca ya da uygulama güncellenince bildirim servisi kendiliğinden başlar. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        EventService.start(c);
    }
}
