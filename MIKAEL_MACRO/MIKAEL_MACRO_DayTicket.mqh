//+------------------------------------------------------------------+
//| MIKAEL_IA_DayTicket.mqh — module ISOLE de validation d'activite  |
//|                                                                  |
//| Usage FTMO actuel (jours de trading minimum SUPPRIMES par FTMO) :|
//|  - CHALLENGE/VERIF : InpEnsureDayTrade=false — module inactif    |
//|    (un micro-lot ouvert/referme n'est pas "replicable" au sens   |
//|    des Risk Management Rules FTMO ; inutile depuis la fin des    |
//|    4 jours minimum).                                             |
//|  - COMPTE FINANCE : InpEnsureDayTrade=true + EveryD=25 —         |
//|    garantit une ouverture tous les 25 jours -> jamais suspendu   |
//|    pour inactivite (regle des 30 jours).                         |
//|  (EveryD=1 = ancien mode challenge, conserve mais obsolete.)     |
//|                                                                  |
//| Technique du micro-lot (fermeture instantanee) :                 |
//|  - lot le plus petit ACCEPTE par le broker (on part du volume    |
//|    minimum et on monte d'un pas si l'ordre est refuse) ;         |
//|  - direction donnee par le MODELE (meilleur R espere du moment); |
//|  - position refermee IMMEDIATEMENT : le P/L de quelques centimes |
//|    suffit a valider le jour, les gains acquis ne sont jamais     |
//|    mis en danger ;                                               |
//|  - magic = InpMagic+1 : invisible pour le cooldown, le time-stop,|
//|    les limites de positions et les stats de la strategie.        |
//|                                                                  |
//| Point d'entree unique : DayTicket_Run(canTrade), appele par      |
//| OnTimer. Tout le reste est prive au module.                      |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Nettoyage : referme immediatement tout micro-lot residuel        |
//| (fermeture instantanee ratee, redemarrage entre-temps...)        |
//+------------------------------------------------------------------+
void DayTicket_Cleanup()
{
   for(int i=PositionsTotal()-1;i>=0;i--){
      PositionGetSymbol(i);
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic+1) continue;
      ulong tk=PositionGetInteger(POSITION_TICKET);
      if(g_trade.PositionClose(tk))
         Print("[DAY-TICKET] micro-lot residuel referme (ticket ",tk,")");
   }
}
//+------------------------------------------------------------------+
//| Debut de la fenetre de reference                                 |
//|  - challenge : minuit FTMO du jour courant                       |
//|  - finance   : il y a InpDayTradeEveryD jours                    |
//+------------------------------------------------------------------+
datetime DayTicket_WindowStart()
{
   if(InpDayTradeEveryD<=1)
      return FtmoDayStartServer();   // minuit FTMO en temps serveur (gere le mode AUTO -1)
   return TimeCurrent()-(datetime)InpDayTradeEveryD*86400;
}
//+------------------------------------------------------------------+
//| Une position a-t-elle deja ete OUVERTE dans la fenetre ?         |
//| (tout magic : les criteres FTMO sont au niveau du COMPTE)        |
//+------------------------------------------------------------------+
bool DayTicket_AlreadyValidated()
{
   datetime since=DayTicket_WindowStart();
   for(int i=PositionsTotal()-1;i>=0;i--){
      PositionGetSymbol(i);
      if((datetime)PositionGetInteger(POSITION_TIME)>=since) return true;
   }
   if(HistorySelect(since,TimeCurrent())){
      for(int h=HistoryDealsTotal()-1;h>=0;h--){
         ulong dl=HistoryDealGetTicket(h);
         if(HistoryDealGetInteger(dl,DEAL_ENTRY)==DEAL_ENTRY_IN) return true;
      }
   }
   return false;
}
//+------------------------------------------------------------------+
//| Direction donnee par le modele : paire libre au meilleur R espere|
//| Retourne false si aucune paire n'est disponible.                 |
//+------------------------------------------------------------------+
bool DayTicket_PickSignal(string &sym, bool &isLong, double &pred)
{
   sym=""; isLong=true; pred=-1e9;
   for(int s=0;s<g_nsym;s++){
      string sy=SYMBOLS[s];
      if(SymbolInfoInteger(sy,SYMBOL_TRADE_MODE)!=SYMBOL_TRADE_MODE_FULL) continue;
      if(SymbolBusy(sy)) continue; // jamais une paire tenue par la strategie (fusion netting)
      MqlRates r4[]; ArraySetAsSeries(r4,false);
      if(CopyRates(sy,InpSignalTF,1,LOOKBACK,r4)<60) continue;
      MqlRates rD[]; ArraySetAsSeries(rD,false);
      if(CopyRates(sy,PERIOD_D1,1,200,rD)<55) continue;
      float f[NFEAT];
      if(!ComputeFeatures(sy,r4,rD,f)) continue;
      double p=PredictONNX(f); if(p<=-900) continue;
      // V4 : direction = signe du score ; meilleur candidat = |score| max
      if(sym=="" || MathAbs(p)>MathAbs(pred)){ pred=p; sym=sy; isLong=(p>0); }
   }
   if(sym=="")
      for(int s=0;s<g_nsym;s++)
         if(!SymbolBusy(SYMBOLS[s]) &&
            SymbolInfoInteger(SYMBOLS[s],SYMBOL_TRADE_MODE)==SYMBOL_TRADE_MODE_FULL)
         { sym=SYMBOLS[s]; isLong=true; pred=0; break; } // repli neutre
   return (sym!="");
}
//+------------------------------------------------------------------+
//| Ouvre le plus petit lot ACCEPTE puis referme immediatement.      |
//| Part du volume minimum ; si le broker refuse pour volume         |
//| invalide, monte d'un pas et reessaie (3 tentatives max).         |
//+------------------------------------------------------------------+
bool DayTicket_Execute(const string sym, const bool isLong, const double pred)
{
   double vol =SymbolInfoDouble(sym,SYMBOL_VOLUME_MIN);
   double step=SymbolInfoDouble(sym,SYMBOL_VOLUME_STEP);
   if(step<=0) step=vol;
   int dg=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
   MqlTick tk; if(!SymbolInfoTick(sym,tk)) return false;

   // SL/TP larges de securite : uniquement au cas ou la fermeture
   // instantanee echouerait (le nettoyage refermera au cycle suivant)
   double pip=(StringFind(sym,"JPY")>=0)?0.01:0.0001;
   double slp=SlPips(sym)*pip;
   double base=isLong?tk.bid:tk.ask;
   double sl=NormalizeDouble(isLong?base-slp:base+slp,dg);
   double tp=NormalizeDouble(isLong?base+InpRR*slp:base-InpRR*slp,dg);

   g_trade.SetExpertMagicNumber(InpMagic+1);
   g_trade.SetTypeFillingBySymbol(sym); // FOK/IOC selon le serveur (anti-10030)
   bool ok=false;
   for(int attempt=0;attempt<3 && !ok;attempt++){
      ok=isLong? g_trade.Buy(vol,sym,0.0,sl,tp,"day-ticket")
               : g_trade.Sell(vol,sym,0.0,sl,tp,"day-ticket");
      if(!ok){
         uint rc=g_trade.ResultRetcode();
         if(rc!=TRADE_RETCODE_INVALID_VOLUME) break; // autre cause : inutile d'insister
         vol+=step;                                  // lot trop petit -> pas suivant
      }
   }
   g_trade.SetExpertMagicNumber(InpMagic);
   if(!ok) return false;

   // fermeture INSTANTANEE : le jour est valide, le P/L de quelques
   // centimes n'expose pas les gains acquis
   ulong deal=g_trade.ResultDeal();
   if(HistoryDealSelect(deal))
      g_trade.PositionClose((ulong)HistoryDealGetInteger(deal,DEAL_POSITION_ID));

   Print("[DAY-TICKET] ",sym," ",(isLong?"Buy":"Sell")," lot=",DoubleToString(vol,2),
         " pred=",DoubleToString(pred,3)," ouvert/referme — jour de trading valide");
   LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(isLong?"Buy":"Sell")+";"+
          DoubleToString(pred,4)+";0;"+DoubleToString(isLong?tk.ask:tk.bid,dg)+";"+
          DoubleToString(vol,2)+";0;DAY_TICKET");
   return true;
}
//+------------------------------------------------------------------+
//| Point d'entree du module — appele a chaque cycle OnTimer         |
//+------------------------------------------------------------------+
void DayTicket_Run(const bool canTrade)
{
   if(InpDryRun || !canTrade) return;
   DayTicket_Cleanup();
   if(!InpEnsureDayTrade || g_halted) return;

   MqlDateTime now; TimeToStruct(TimeCurrent(),now);
   if(now.day_of_week==0 || now.day_of_week==6) return;
   // regle FTMO gap-trading : aucune ouverture a moins de 2h de la cloture
   // hebdomadaire (vendredi ~23h57 serveur) — meme garde que la strategie
   if(now.day_of_week==5 && now.hour>=InpNoFridayAfter) return;
   if(now.hour<InpDayTradeHour) return;
   if(DayTicket_AlreadyValidated()) return;

   // priorite ABSOLUE a la strategie : si un signal reel est encore en
   // file d'attente (spread/halt), on lui laisse sa chance — son execution
   // validera le jour bien mieux qu'un micro-trade. Le ticket ne part que
   // lorsqu'il n'y a VRAIMENT plus aucune opportunite en jeu.
   for(int s=0;s<g_nsym;s++) if(g_pendActive[s]) return;

   string sym; bool isLong; double pred;
   if(!DayTicket_PickSignal(sym,isLong,pred)) return;

   // spread correct exige, sauf en toute fin de journee (le jour prime)
   double pip=(StringFind(sym,"JPY")>=0)?0.01:0.0001;
   MqlTick tk; if(!SymbolInfoTick(sym,tk)) return;
   if(now.hour<23 && (tk.ask-tk.bid)/pip>RefSpreadPips(sym)*InpMaxSpreadMult) return;

   DayTicket_Execute(sym,isLong,pred);
}
//+------------------------------------------------------------------+
