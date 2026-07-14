//+------------------------------------------------------------------+
//| ExportCalendar.mq5 — exporte l'historique du calendrier MQL5     |
//| (2015->aujourd'hui, devises des 8 paires) vers CSV.              |
//| Sortie : MQL5\Files\calendar_history.csv                         |
//| Usage : glisser le script sur n'importe quel graphique.          |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict
input datetime InpFrom = D'2015.06.01';

void OnStart()
{
   string curr[] = {"USD","EUR","JPY","GBP","AUD","NZD"};
   int fh = FileOpen("calendar_history.csv", FILE_WRITE|FILE_CSV|FILE_ANSI, ';');
   if(fh == INVALID_HANDLE){ Print("FileOpen err ", GetLastError()); return; }
   FileWrite(fh, "time","currency","event_id","name","importance",
                 "actual","forecast","previous","revised");
   datetime to = TimeCurrent();
   long total = 0;
   for(int c = 0; c < ArraySize(curr); c++)
   {
      // decoupe par tranches de 1 an + 3 tentatives (la base calendrier
      // se synchronise a la demande : la 1ere requete peut timeouter, err 5401)
      for(datetime a = InpFrom; a < to; a += 365*24*3600)
      {
      datetime b = MathMin(a + 365*24*3600, to);
      MqlCalendarValue vals[];
      int n = -1;
      for(int att = 0; att < 3 && n <= 0; att++)
      {
         ResetLastError();
         n = CalendarValueHistory(vals, a, b, NULL, curr[c]);
         if(n <= 0){ Print(curr[c]," ",TimeToString(a,TIME_DATE),": tentative ",att+1," err ",GetLastError()); Sleep(5000); }
      }
      if(n <= 0) continue;
      for(int i = 0; i < ArraySize(vals); i++)
      {
         MqlCalendarEvent ev;
         if(!CalendarEventById(vals[i].event_id, ev)) continue;
         if(ev.importance == CALENDAR_IMPORTANCE_NONE) continue;
         FileWrite(fh,
            TimeToString(vals[i].time, TIME_DATE|TIME_MINUTES),
            curr[c], (long)ev.id, ev.name, (int)ev.importance,
            vals[i].HasActualValue()   ? DoubleToString(vals[i].GetActualValue(),4)   : "",
            vals[i].HasForecastValue() ? DoubleToString(vals[i].GetForecastValue(),4) : "",
            vals[i].HasPreviousValue() ? DoubleToString(vals[i].GetPreviousValue(),4) : "",
            vals[i].HasRevisedValue()  ? DoubleToString(vals[i].GetRevisedValue(),4)  : "");
         total++;
      }
      Print(curr[c], " ", TimeToString(a,TIME_DATE), ": ", ArraySize(vals), " valeurs");
      }
   }
   FileClose(fh);
   Print("Export termine: ", total, " lignes -> MQL5\\Files\\calendar_history.csv");
}
