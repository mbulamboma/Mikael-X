//+------------------------------------------------------------------+
//| MIKAEL_DONCHIAN.mq5 — 2 moteurs (Donchian / Scalp), armature FTMO|
//| v1.02 : InpStrategy = STRAT_DONCHIAN (defaut) ou STRAT_SCALP.    |
//|  SCALP = pullback sur EMA rapide (EMA8/21) dans le sens du biais |
//|  court terme + reprise ; SL serre (1.2xATR), TP=1.5xSL, filtre   |
//|  de tendance EMA200 + ADX en couche commune. Regler InpSignalTF  |
//|  sur M5 ou M15. ⚠️ scalp FX : le spread+commission mange une part|
//|  enorme d'un petit TP — valider en dry-run/testeur AVANT le reel.|
//| Turtle / Donchian Channel, armature FTMO                         |
//| Derive de MIKAEL_IA v1.79 : TOUTE la gestion du risque FTMO est  |
//| conservee a l'identique ; le moteur de prediction ONNX est       |
//| remplace par la strategie Donchian (Turtle Traders) :            |
//|                                                                  |
//|  ENTREE  : cloture au-dela du canal InpEntryPeriod (defaut 20)   |
//|  SORTIE  : cloture au-dela du canal oppose InpExitPeriod (10) ;  |
//|            si InpStopAndReverse, position inverse ouverte si les |
//|            filtres l'autorisent (jamais contre la tendance EMA)  |
//|  STOP    : InpSLMode = MEDIAN (ligne mediane, defaut) /          |
//|            OPPOSITE (bande opposee) / ATR (InpSLATRMult x ATR14) |
//|  TP      : InpRR=0 (defaut) -> PAS de TP, la sortie de canal     |
//|            laisse courir la tendance (esprit Turtle) ;           |
//|            InpRR>0 -> TP = RR x distance SL                      |
//|                                                                  |
//|  FILTRES anti-fausse-cassure (ranges) :                          |
//|   - EMA InpEMAPeriod (200) sur le TF de signal : long ssi        |
//|     close>EMA, short ssi close<EMA (0=off)                       |
//|   - SMA InpTrendMAD1 (200) sur D1 : tendance de fond (0=off)     |
//|   - ADX14 >= InpMinADX (20) : force de tendance requise (0=off)  |
//|   - RSI14 : pas d'achat en surachat (> InpRSICap, 70) ni de      |
//|     vente en survente (< 100-InpRSICap) (InpRSICap=100 -> off)   |
//|                                                                  |
//| ARMATURE FTMO (copie conforme MIKAEL_IA v1.79) :                 |
//|  - sizing exact tick value, risque % par trade                   |
//|  - budget de perte journalier PROSPECTIF (flottant + risque du   |
//|    nouveau trade inclus), ancre balance minuit CE(S)T (DST auto) |
//|  - Max Loss statique vs solde initial (InpInitialBalance)        |
//|  - halt persistant (GlobalVariables magic+login, rafraichi)      |
//|  - objectif de profit -> stoppe les entrees (gains acquis)       |
//|  - regle gap-trading : rien vendredi apres InpNoFridayAfter      |
//|  - file d'attente spread 90 min + garde anti-derive 0.25xSL      |
//|  - cooldown apres perte (defaut 0 ici : le stop&reverse doit     |
//|    pouvoir s'executer ; les filtres EMA/ADX tiennent le range)   |
//|  - time-stop optionnel (defaut 0 : une tendance se laisse courir)|
//|  - fill type par symbole (anti-10030), retry requote, stops level|
//|  NOTE : pas de module day-ticket (FTMO a supprime les jours mini;|
//|  sur compte FINANCE, re-attacher MIKAEL_IA ou ajouter le module) |
//+------------------------------------------------------------------+
#property copyright "Mbula"
#property version   "1.14"
#property strict

#include <Trade\Trade.mqh>

//--- mode de placement du stop loss
enum ENUM_SL_MODE
{
   SL_MEDIAN=0,    // ligne mediane du canal d'entree
   SL_OPPOSITE=1,  // bande opposee du canal d'entree
   SL_ATR=2        // InpSLATRMult x ATR14
};

//--- choix du moteur de signaux
enum ENUM_STRATEGY
{
   STRAT_DONCHIAN=0,  // Turtle : cassure de canal (defaut, tendance)
   STRAT_SCALP=1      // Scalp : pullback sur EMA rapide dans le sens de la tendance
};

//--- INPUTS strategie
input ENUM_STRATEGY InpStrategy = STRAT_DONCHIAN; // MOTEUR : Donchian (tendance) ou Scalp (pullback EMA). Pour scalper: mettre STRAT_SCALP + InpSignalTF=M5/M15
input ENUM_TIMEFRAMES InpSignalTF   = PERIOD_H1; // timeframe des signaux — H1 = config forward-test (instance scalp: M15 + magic 20260715)
input int    InpEntryPeriod   = 20;        // canal d'ENTREE (cassure du plus haut/bas N barres)
input int    InpExitPeriod    = 10;        // canal de SORTIE (canal oppose N barres)
input ENUM_SL_MODE InpSLMode  = SL_MEDIAN; // placement du SL initial
input double InpSLATRMult     = 2.0;       // multiplicateur ATR si InpSLMode=SL_ATR
input double InpRR            = 0.0;       // 0 = pas de TP (sortie par canal) ; >0 : TP = RR x SL
input bool   InpStopAndReverse= true;      // sortie de canal -> position inverse (si filtres OK)
//--- FILTRES anti-fausse-cassure
input int    InpEMAPeriod     = 200;       // EMA tendance sur TF de signal (0=off) — SEUL filtre de tendance par defaut
input int    InpTrendMAD1     = 0;         // SMA D1 tendance de fond (0=off) — desactive : double filtre trop strict (etouffait les cassures)
input double InpMinADX        = 15.0;      // ADX14 minimum (0=off) — 15 laisse passer les cassures naissantes (20 = trop tardif sur FX)
input double InpRSICap        = 100.0;     // pas d'achat si RSI>cap, pas de vente si RSI<100-cap (100=OFF). ⚠️ un cap<100 CONTREDIT la cassure : un plus-haut 20 barres a par nature un RSI eleve — le RSI rejetait les meilleures tendances
//--- FILTRE SENTIMENT/MACRO (macro_features.csv ecrit par v4_macro\macro_service.py)
input double InpSentThreshold = 0.15;      // veto : pas de Buy si sent(base)-sent(quote) < -seuil, pas de Sell si > +seuil. 0=off
input int    InpSentMaxAgeH   = 12;        // fraicheur max du fichier (h) ; perime/absent = filtre INACTIF (log) — jamais bloquant
//--- PARAMETRES SCALP (utilises seulement si InpStrategy=STRAT_SCALP)
input int    InpScalpEMAfast  = 8;         // EMA rapide (declencheur pullback)
input int    InpScalpEMAslow  = 21;        // EMA lente (biais court terme)
input double InpScalpSLATR    = 1.2;       // SL = mult x ATR14 (stop serre = scalp)
input double InpScalpRR        = 0.0;      // 0 = PAS de TP (sortie au flip du biais EMA8/21 — regle CTA : la queue droite reste libre) ; >0 : TP = RR x SL
input int    InpScalpRSIfloor = 45;        // long: RSI>=floor ; short: RSI<=100-floor (momentum, evite les rebonds morts). 0=off
//--- ARMATURE FTMO (identique MIKAEL_IA)
input double InpRiskCashFixed  = 0.0;      // 0 = sizing INSTITUTIONNEL (fraction fixe du capital via InpRiskPerTrade). >0 = risque fixe en $ (mode nano-test)
input double InpRiskPerTrade   = 0.005;    // risque par trade en % de l'equity (0.5%) — utilise seulement si InpRiskCashFixed=0
input int    InpMaxHoldHours   = 0;        // time-stop (0=off : la sortie de canal gere)
input double InpDailyStopPct   = 0.035;    // budget de perte journalier (FTMO 5% - marge)
input double InpMaxDDPct       = 0.07;     // halt total (FTMO 10% - marge)
input int    InpMaxConcurrent  = 3;        // positions simultanees max
input int    InpMaxPerCcy      = 2;        // positions max par devise
input int    InpNoFridayAfter  = 22;       // pas d'entree vendredi apres (h srv) — regle FTMO gap-trading
input double InpMaxSpreadPips  = 1.0;      // spread max en pips (EURUSD ~0.2 chez FTMO)
input int    InpCooldownHours  = 0;        // pause par paire apres une perte (0=off, laisse le S&R travailler)
input int    InpDayResetOffsetH= -1;       // -1 = AUTO (minuit CE(S)T) ; >=0 = decalage manuel serveur->FTMO
input double InpMaxDriftSL     = 0.25;     // abandon d'un signal differe si derive prix > x*SL
input double InpMinLotRiskMult = 2.0;      // skip si le lot min risque > mult x risque cible
input double InpInitialBalance = 10000;    // solde initial FTMO : ref STATIQUE Max Loss + target ; 0 = repli peak
input double InpTargetPct      = 0.10;     // objectif de profit : stoppe les entrees une fois atteint
input bool   InpDryRun         = false;    // false = ordres reels (forward-test DEMO). ⚠️ verifier que le compte connecte est bien un DEMO
input string InpSymbols        = "EURUSD,GBPUSD,USDJPY,AUDUSD,NZDUSD,EURJPY,GBPJPY,AUDJPY"; // les 8 majeures du plan forward
input long   InpMagic          = 20260713;

#define LOOKBACK 600   // barres TF signal chargees (>= EMA200 + marge de warm-up)

//--- etat global
string   SYMBOLS[];
int      g_nsym = 0;
CTrade   g_trade;
datetime g_lastBar[];
double   g_dayAnchor = 0.0;
double   g_peak      = 0.0;
int      g_dayOfYear = -1;
int      g_fileLog   = INVALID_HANDLE;
bool     g_halted    = false;
string   g_gvPeak, g_gvHalt, g_gvDayA, g_gvDayD, g_gvCool;
// signaux en attente (spread large -> retry 90 min)
bool     g_pendActive[];
bool     g_pendLong[];
double   g_pendSlDist[];   // distance SL au moment du signal (prix)
datetime g_pendExpiry[];
double   g_pendRefPx[];
datetime g_coolUntil[];

int SymIndex(const string s)
{
   // borne par la taille REELLE du tableau : au re-init (changement d'input),
   // g_nsym peut etre momentanement > ArraySize(SYMBOLS) -> array out of range
   // qui TUAIT l'EA jusqu'au rechargement (bug v1.10 observe le 14 juil)
   int n=MathMin(g_nsym,ArraySize(SYMBOLS));
   for(int i=0;i<n;i++) if(SYMBOLS[i]==s) return i;
   return -1;
}
//+------------------------------------------------------------------+
//| Parse InpSymbols (CSV) — pas de contrainte de modele ici :       |
//| toute paire coteee par le broker est acceptee                    |
//+------------------------------------------------------------------+
int ParseSymbols(const string csv)
{
   ArrayResize(SYMBOLS,0);
   g_nsym=0;                 // sync IMMEDIATE avec le tableau vide (anti-crash re-init)
   string parts[];
   int n=StringSplit(csv,',',parts);
   for(int i=0;i<n;i++){
      string s=parts[i];
      StringTrimLeft(s); StringTrimRight(s);
      StringToUpper(s);
      if(s=="") continue;
      if(SymIndex(s)>=0) continue;
      if(!SymbolSelect(s,true)){ Print("[SYMBOLS] introuvable chez le broker, ignoree: '",s,"'"); continue; }
      int m=ArraySize(SYMBOLS); ArrayResize(SYMBOLS,m+1); SYMBOLS[m]=s;
      g_nsym=ArraySize(SYMBOLS);
   }
   g_nsym=ArraySize(SYMBOLS);
   return g_nsym;
}
//+------------------------------------------------------------------+
//| Indicateurs (Wilder ewm alpha=1/n, comme MIKAEL_IA)              |
//+------------------------------------------------------------------+
void EwmAlpha(const double &src[], const int n, double &dst[])
{
   int sz = ArraySize(src); ArrayResize(dst, sz);
   double a = 1.0/n;
   dst[0] = src[0];
   for(int i=1;i<sz;i++) dst[i] = a*src[i] + (1.0-a)*dst[i-1];
}
double EmaLast(const double &src[], const int n)
{
   int sz=ArraySize(src); if(n<=0 || sz<n) return 0.0;
   double a=2.0/(n+1.0), e=src[0];
   for(int i=1;i<sz;i++) e=a*src[i]+(1.0-a)*e;
   return e;
}
double SmaLastRates(const MqlRates &r[], const int period)
{
   int n=ArraySize(r);
   if(period<=0 || n<period) return 0.0;
   double s=0.0;
   for(int i=n-period;i<n;i++) s+=r[i].close;
   return s/period;
}
//+------------------------------------------------------------------+
//| Photographie des indicateurs sur la DERNIERE bougie FERMEE.      |
//| Les canaux EXCLUENT la bougie de signal (cassure = close au-dela |
//| du canal des N barres PRECEDENTES — definition Turtle).          |
//+------------------------------------------------------------------+
struct Indi
{
   double close;                  // cloture de la bougie de signal
   double hiE, loE, mid;          // canal d'entree (InpEntryPeriod) + mediane
   double hiX, loX;               // canal de sortie (InpExitPeriod)
   double ema;                    // EMA tendance (0 si off)
   double atr, rsi, adx;
   // --- scalp : EMA rapide/lente sur la bougie de signal ET la precedente ---
   double emaF, emaS;             // EMA rapide / lente a la bougie de signal
   double emaFprev;               // EMA rapide a la bougie precedente
   double closePrev;              // cloture de la bougie precedente
};
//--- EMA a un index donne (recalcul depuis le debut de la fenetre)
double EmaAt(const double &src[], const int n, const int idx)
{
   if(n<=0 || idx<0 || idx>=ArraySize(src)) return 0.0;
   double a=2.0/(n+1.0), e=src[0];
   for(int i=1;i<=idx;i++) e=a*src[i]+(1.0-a)*e;
   return e;
}
bool ComputeIndi(const MqlRates &r[], Indi &v)
{
   int n=ArraySize(r);
   int need=MathMax(MathMax(InpEntryPeriod,InpExitPeriod)+2,
                    MathMax(InpEMAPeriod+10,60));
   if(n<need) return false;
   int i=n-1;                      // derniere bougie fermee
   v.close=r[i].close;

   v.hiE=-DBL_MAX; v.loE=DBL_MAX;
   for(int k=i-InpEntryPeriod;k<i;k++){ v.hiE=MathMax(v.hiE,r[k].high); v.loE=MathMin(v.loE,r[k].low); }
   v.mid=(v.hiE+v.loE)/2.0;
   v.hiX=-DBL_MAX; v.loX=DBL_MAX;
   for(int k=i-InpExitPeriod;k<i;k++){ v.hiX=MathMax(v.hiX,r[k].high); v.loX=MathMin(v.loX,r[k].low); }

   double close[],tr[],gain[],loss[];
   ArrayResize(close,n); ArrayResize(tr,n); ArrayResize(gain,n); ArrayResize(loss,n);
   for(int k=0;k<n;k++) close[k]=r[k].close;
   tr[0]=r[0].high-r[0].low; gain[0]=0; loss[0]=0;
   for(int k=1;k<n;k++){
      double a=r[k].high-r[k].low,
             b=MathAbs(r[k].high-r[k-1].close),
             c=MathAbs(r[k].low -r[k-1].close);
      tr[k]=MathMax(a,MathMax(b,c));
      double d=close[k]-close[k-1];
      gain[k]=(d>0)?d:0; loss[k]=(d<0)?-d:0;
   }
   double atr[]; EwmAlpha(tr,14,atr);
   double ag[],al[]; EwmAlpha(gain,14,ag); EwmAlpha(loss,14,al);
   v.atr=atr[i];
   v.rsi=(al[i]>0)? 100.0-100.0/(1.0+ag[i]/al[i]) : 100.0;

   // ADX14 (Wilder)
   double pdm[],mdm[]; ArrayResize(pdm,n); ArrayResize(mdm,n); pdm[0]=0; mdm[0]=0;
   for(int k=1;k<n;k++){
      double up=r[k].high-r[k-1].high, dn=r[k-1].low-r[k].low;
      pdm[k]=(up>dn && up>0)?up:0; mdm[k]=(dn>up && dn>0)?dn:0;
   }
   double pdis[],mdis[]; EwmAlpha(pdm,14,pdis); EwmAlpha(mdm,14,mdis);
   double dx[]; ArrayResize(dx,n);
   for(int k=0;k<n;k++){
      double pdi=(atr[k]>0)?100.0*pdis[k]/atr[k]:0, mdi=(atr[k]>0)?100.0*mdis[k]/atr[k]:0;
      dx[k]=((pdi+mdi)>0)?100.0*MathAbs(pdi-mdi)/(pdi+mdi):0;
   }
   double adx[]; EwmAlpha(dx,14,adx);
   v.adx=adx[i];

   v.ema=(InpEMAPeriod>0)? EmaLast(close,InpEMAPeriod) : 0.0;

   // --- scalp : EMA rapide/lente a i et i-1 (pullback = croisement close/emaF) ---
   v.emaF     = EmaAt(close,InpScalpEMAfast,i);
   v.emaS     = EmaAt(close,InpScalpEMAslow,i);
   v.emaFprev = EmaAt(close,InpScalpEMAfast,i-1);
   v.closePrev= close[i-1];

   if(v.atr<=0) return false;
   return true;
}
//+------------------------------------------------------------------+
//| SENTIMENT : lit macro_features.csv (macro_service.py).           |
//| Retourne true + sent = sent24(base)-sent24(quote) si le fichier  |
//| est present ET frais ; false sinon (filtre inactif, fail-open :  |
//| une panne du service Python ne doit pas paralyser l'EA).         |
//+------------------------------------------------------------------+
bool GetPairSentiment(const string sym, double &sent)
{
   sent=0.0;
   int h=FileOpen("macro_features.csv",FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h==INVALID_HANDLE) return false;
   string base=StringSubstr(sym,0,3), quote=StringSubstr(sym,3,3);
   double sBase=0, sQuote=0; bool gotB=false, gotQ=false;
   datetime updated=0;
   FileReadString(h);                                  // saute l'en-tete
   while(!FileIsEnding(h)){
      string line=FileReadString(h);
      if(line=="") continue;
      string f[]; if(StringSplit(line,';',f)<8) continue;
      if(f[0]==base) { sBase =StringToDouble(f[1]); gotB=true; }
      if(f[0]==quote){ sQuote=StringToDouble(f[1]); gotQ=true; }
      int nf=ArraySize(f);
      string ts=f[nf-1]; StringReplace(ts,"-",".");    // timestamp = DERNIERE colonne (robuste aux ajouts de colonnes)
      updated=StringToTime(ts);
   }
   FileClose(h);
   if(!gotB || !gotQ || updated==0) return false;
   if(TimeGMT()-updated>(long)InpSentMaxAgeH*3600){
      static datetime lastWarn=0;
      if(TimeCurrent()-lastWarn>3600){ lastWarn=TimeCurrent();
         Print("[SENT] macro_features.csv PERIME (",(TimeGMT()-updated)/3600,
               "h) — filtre sentiment INACTIF (relancer macro_service.py)"); }
      return false;
   }
   sent=sBase-sQuote;
   return true;
}
//+------------------------------------------------------------------+
//| Filtres de tendance/force : autorisent-ils la direction demandee |
//| reason = premier filtre bloquant (log)                           |
//+------------------------------------------------------------------+
bool FiltersAllow(const string sym, const bool longSig, const MqlRates &rD[], const Indi &v, string &reason)
{
   // --- veto sentiment : jamais CONTRE le flux macro/news (fail-open) ---
   if(InpSentThreshold>0){
      double sent;
      if(GetPairSentiment(sym,sent)){
         if( longSig && sent<-InpSentThreshold){ reason="sent_contre("+DoubleToString(sent,2)+")"; return false; }
         if(!longSig && sent> InpSentThreshold){ reason="sent_contre("+DoubleToString(sent,2)+")"; return false; }
      }
   }
   if(InpEMAPeriod>0 && v.ema>0){
      if( longSig && v.close<=v.ema){ reason="ema_contre"; return false; }
      if(!longSig && v.close>=v.ema){ reason="ema_contre"; return false; }
   }
   if(InpTrendMAD1>0){
      double maD=SmaLastRates(rD,InpTrendMAD1);
      if(maD<=0){ reason="d1_insuffisant"; return false; }   // pas assez d'historique -> on bloque
      double cD=rD[ArraySize(rD)-1].close;
      if( longSig && cD<=maD){ reason="d1_contre"; return false; }
      if(!longSig && cD>=maD){ reason="d1_contre"; return false; }
   }
   if(InpMinADX>0 && v.adx<InpMinADX){ reason="adx_faible"; return false; }
   if(InpRSICap<100){
      if( longSig && v.rsi>InpRSICap)        { reason="rsi_surachat"; return false; }
      if(!longSig && v.rsi<(100.0-InpRSICap)){ reason="rsi_survente"; return false; }
   }
   reason="";
   return true;
}
//+------------------------------------------------------------------+
//| Distance du SL initial (en prix) selon le mode choisi            |
//+------------------------------------------------------------------+
double SlDistance(const bool longSig, const Indi &v)
{
   double d=0.0;
   switch(InpSLMode){
      case SL_MEDIAN:   d=longSig? (v.close-v.mid) : (v.mid-v.close); break;
      case SL_OPPOSITE: d=longSig? (v.close-v.loE) : (v.hiE-v.close); break;
      case SL_ATR:      d=InpSLATRMult*v.atr; break;
   }
   // garde-fou : jamais moins de 0.5 x ATR (une cassure qui colle a la
   // mediane donnerait un SL minuscule -> lot enorme -> risque reel >> cible)
   return MathMax(d,0.5*v.atr);
}
//+------------------------------------------------------------------+
//| SCALP : pullback sur EMA rapide dans le sens du biais court terme|
//| Long  : emaF>emaS (biais haussier) ET la bougie PRECEDENTE a     |
//|         cloture SOUS l'emaF (repli) ET la bougie de signal        |
//|         re-cloture AU-DESSUS de l'emaF (reprise) ET RSI>=floor    |
//| Short : symetrique. Retourne +1 / -1 / 0.                        |
//| La tendance de fond (EMA200/D1) et l'ADX sont ajoutes ensuite    |
//| par FiltersAllow (couche commune).                               |
//+------------------------------------------------------------------+
int ScalpSignal(const Indi &v, string &reason)
{
   reason="";
   if(v.emaF<=0 || v.emaS<=0 || v.emaFprev<=0) return 0;
   bool upBias = (v.emaF>v.emaS);
   bool dnBias = (v.emaF<v.emaS);

   bool longSetup  = upBias && (v.closePrev<v.emaFprev) && (v.close>v.emaF);
   bool shortSetup = dnBias && (v.closePrev>v.emaFprev) && (v.close<v.emaF);
   if(!longSetup && !shortSetup) return 0;

   if(InpScalpRSIfloor>0){
      if(longSetup  && v.rsi<InpScalpRSIfloor)          { reason="scalp_rsi_mou";  return 0; }
      if(shortSetup && v.rsi>(100.0-InpScalpRSIfloor))  { reason="scalp_rsi_mou";  return 0; }
   }
   return longSetup ? 1 : -1;
}
//+------------------------------------------------------------------+
//| Portefeuille                                                     |
//+------------------------------------------------------------------+
int CcyCount(const string ccy)
{
   int c=0;
   for(int i=PositionsTotal()-1;i>=0;i--){
      string sym=PositionGetSymbol(i);
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      if(StringSubstr(sym,0,3)==ccy || StringSubstr(sym,3,3)==ccy) c++;
   }
   return c;
}
int MagicPositions()
{
   int c=0;
   for(int i=PositionsTotal()-1;i>=0;i--){
      PositionGetSymbol(i);
      if(PositionGetInteger(POSITION_MAGIC)==InpMagic) c++;
   }
   return c;
}
bool SymbolBusy(const string sym)
{
   for(int i=PositionsTotal()-1;i>=0;i--){
      if(PositionGetSymbol(i)==sym && PositionGetInteger(POSITION_MAGIC)==InpMagic) return true;
   }
   return false;
}
//+------------------------------------------------------------------+
//| Sizing EXACT (copie MIKAEL_IA)                                   |
//+------------------------------------------------------------------+
double LossPerLotAtSL(const string sym, const bool longSig, const double price, const double sl)
{
   double loss=0.0;
   if(OrderCalcProfit(longSig?ORDER_TYPE_BUY:ORDER_TYPE_SELL,sym,1.0,price,sl,loss) && loss<0)
      return -loss;
   double tickVal=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tickVal<=0) tickVal=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE);
   double tickSz=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_SIZE);
   if(tickVal<=0||tickSz<=0) return 0.0;
   return MathAbs(price-sl)/tickSz*tickVal;
}
double CalcLots(const string sym, const bool longSig, const double price,
                const double sl, const double riskCash, string &why)
{
   double perLot=LossPerLotAtSL(sym,longSig,price,sl);
   if(perLot<=0){ why="tick_value_indisponible"; return 0.0; }
   double lots=riskCash/perLot;
   double step=SymbolInfoDouble(sym,SYMBOL_VOLUME_STEP);
   double vmin=SymbolInfoDouble(sym,SYMBOL_VOLUME_MIN);
   double vmax=SymbolInfoDouble(sym,SYMBOL_VOLUME_MAX);
   if(step<=0) step=0.01;
   lots=MathFloor(lots/step)*step;
   if(lots>vmax) lots=vmax;
   if(lots<vmin){
      if(vmin*perLot>riskCash*InpMinLotRiskMult){ why="lot_min_sur_risque"; return 0.0; }
      lots=vmin;
   }
   double margin=0.0;
   if(OrderCalcMargin(longSig?ORDER_TYPE_BUY:ORDER_TYPE_SELL,sym,lots,price,margin)){
      if(margin*1.5>AccountInfoDouble(ACCOUNT_MARGIN_FREE)){ why="marge_insuffisante"; return 0.0; }
   }
   why="";
   return lots;
}
//+------------------------------------------------------------------+
//| Risque flottant pire-cas (budget journalier prospectif FTMO)     |
//+------------------------------------------------------------------+
double OpenRiskCash()
{
   double total=0.0;
   for(int i=PositionsTotal()-1;i>=0;i--){
      string sym=PositionGetSymbol(i);
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      double sl=PositionGetDouble(POSITION_SL);
      if(sl<=0) continue;
      long   ptype=PositionGetInteger(POSITION_TYPE);
      bool   isLong=(ptype==POSITION_TYPE_BUY);
      double cur=PositionGetDouble(POSITION_PRICE_CURRENT);
      if((isLong && cur<=sl) || (!isLong && cur>=sl)) continue;
      double vol=PositionGetDouble(POSITION_VOLUME);
      total+=LossPerLotAtSL(sym,isLong,cur,sl)*vol;
   }
   return total;
}
bool StopsValid(const string sym, const double price, const double sl, const double tp)
{
   double pt=SymbolInfoDouble(sym,SYMBOL_POINT);
   double minDist=SymbolInfoInteger(sym,SYMBOL_TRADE_STOPS_LEVEL)*pt;
   if(MathAbs(price-sl)<minDist) return false;
   if(tp>0 && MathAbs(price-tp)<minDist) return false;
   return true;
}
//+------------------------------------------------------------------+
void EnforceTimeStop()
{
   if(InpMaxHoldHours<=0) return;
   for(int i=PositionsTotal()-1;i>=0;i--){
      string sym=PositionGetSymbol(i);
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      datetime opened=(datetime)PositionGetInteger(POSITION_TIME);
      if(TimeCurrent()-opened>=(long)InpMaxHoldHours*3600){
         ulong ticket=PositionGetInteger(POSITION_TICKET);
         if(g_trade.PositionClose(ticket))
            Print("[TIME-STOP] ",sym," fermee apres ",InpMaxHoldHours,"h (ticket ",ticket,")");
         else
            Print("[TIME-STOP] echec fermeture ",sym," ret=",g_trade.ResultRetcode());
      }
   }
}
//+------------------------------------------------------------------+
//| Heure FTMO CE(S)T (copie MIKAEL_IA : DST europeen auto)          |
//+------------------------------------------------------------------+
bool IsEuDst(const datetime gmt)
{
   MqlDateTime d; TimeToStruct(gmt,d);
   MqlDateTime x;
   datetime mar=StringToTime(StringFormat("%04d.03.31 01:00",d.year));
   TimeToStruct(mar,x); datetime dstStart=mar-(x.day_of_week%7)*86400;
   datetime oct=StringToTime(StringFormat("%04d.10.31 01:00",d.year));
   TimeToStruct(oct,x); datetime dstEnd=oct-(x.day_of_week%7)*86400;
   return (gmt>=dstStart && gmt<dstEnd);
}
datetime FtmoTime()
{
   if(InpDayResetOffsetH>=0)
      return TimeCurrent()-(long)InpDayResetOffsetH*3600;
   datetime gmt=TimeGMT();
   return gmt+(IsEuDst(gmt)?2:1)*3600;
}
int FtmoDayOfYear()
{
   MqlDateTime d; TimeToStruct(FtmoTime(),d);
   return d.day_of_year;
}
//+------------------------------------------------------------------+
void LogRow(const string line)
{
   if(g_fileLog==INVALID_HANDLE) return;
   FileSeek(g_fileLog,0,SEEK_END);
   FileWriteString(g_fileLog,line+"\n");
   FileFlush(g_fileLog);
}
//+------------------------------------------------------------------+
//| Tente l'entree. true = signal CONSOMME (execute ou rejete),      |
//| false = a REESSAYER (spread). slDist = distance SL du signal.    |
//+------------------------------------------------------------------+
bool TryEnter(const string sym, const bool longSig, const double slDist,
              const double adx, const bool canTrade, const double refPx=0.0)
{
   double pip=(StringFind(sym,"JPY")>=0)?0.01:0.0001;
   MqlTick tick; if(!SymbolInfoTick(sym,tick)) return false;
   double price=longSig?tick.ask:tick.bid;

   string row=TimeToString(TimeCurrent())+";"+sym+";"+(longSig?"Buy":"Sell")+";"+
              DoubleToString(adx,1)+";1;"+
              DoubleToString(price,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS))+";";

   // garde anti-derive : signal differe dont le prix a trop bouge -> abandon
   if(refPx>0 && MathAbs(price-refPx)>InpMaxDriftSL*slDist)
   { LogRow(row+"0;"+(InpDryRun?"1":"0")+";price_drift"); return true; }

   // spread trop large -> retry
   double sprPips=(tick.ask-tick.bid)/pip;
   if(sprPips>InpMaxSpreadPips) return false;

   // contraintes portefeuille
   int idx=SymIndex(sym);
   if(idx>=0 && TimeCurrent()<g_coolUntil[idx])
   { LogRow(row+"0;"+(InpDryRun?"1":"0")+";cooldown"); return true; }
   if(MagicPositions()>=InpMaxConcurrent){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";max_conc"); return true; }
   if(SymbolBusy(sym)){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";sym_busy"); return true; }
   if(CcyCount(StringSubstr(sym,0,3))>=InpMaxPerCcy ||
      CcyCount(StringSubstr(sym,3,3))>=InpMaxPerCcy)
   { LogRow(row+"0;"+(InpDryRun?"1":"0")+";ccy_corr"); return true; }

   // SL/TP ancres sur le prix de SORTIE (bid=long, ask=short)
   // RR selon le moteur : Donchian=InpRR (0=pas de TP, sortie canal) ; Scalp=InpScalpRR
   double rr=(InpStrategy==STRAT_SCALP)?InpScalpRR:InpRR;
   int digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
   double base=longSig?tick.bid:tick.ask;
   double sl=NormalizeDouble(longSig?base-slDist:base+slDist,digits);
   double tp=(rr>0)? NormalizeDouble(longSig?base+rr*slDist:base-rr*slDist,digits) : 0.0;
   if(!StopsValid(sym,price,sl,tp)){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";stops_level"); return true; }

   // sizing exact — risque en $ FIXE (InpRiskCashFixed>0) sinon % de l'equity
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   string why="";
   double riskCash=(InpRiskCashFixed>0)? InpRiskCashFixed : eq*InpRiskPerTrade;
   double lots=CalcLots(sym,longSig,price,sl,riskCash,why);
   if(lots<=0){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";"+why); return true; }

   // budget de perte journalier PROSPECTIF (regle FTMO)
   double dayPl=(g_dayAnchor>0)?(eq-g_dayAnchor)/g_dayAnchor:0;
   double worstDay=(g_dayAnchor>0)? dayPl-(OpenRiskCash()+riskCash)/g_dayAnchor : 0;
   if(worstDay<=-InpDailyStopPct)
   { LogRow(row+"0;"+(InpDryRun?"1":"0")+";daily_budget_"+DoubleToString(worstDay*100,2)); return true; }

   if(InpDryRun){
      Print("[DRY] SIGNAL ",sym," ",(longSig?"Buy":"Sell")," adx=",DoubleToString(adx,1),
            " sl_dist=",DoubleToString(slDist/pip,1),"p lots=",lots);
      LogRow(row+DoubleToString(lots,2)+";1;DRY_SIGNAL");
      return true;
   }
   if(!canTrade){ LogRow(row+"0;0;trade_non_autorise"); return true; }

   g_trade.SetTypeFillingBySymbol(sym);
   bool ok=longSig? g_trade.Buy(lots,sym,price,sl,tp,"MIKAEL_DONCHIAN")
                  : g_trade.Sell(lots,sym,price,sl,tp,"MIKAEL_DONCHIAN");
   uint rc=g_trade.ResultRetcode();
   if(!ok && (rc==TRADE_RETCODE_REQUOTE || rc==TRADE_RETCODE_PRICE_CHANGED || rc==TRADE_RETCODE_PRICE_OFF)){
      if(SymbolInfoTick(sym,tick)){
         price=longSig?tick.ask:tick.bid;
         base=longSig?tick.bid:tick.ask;
         sl=NormalizeDouble(longSig?base-slDist:base+slDist,digits);
         tp=(rr>0)? NormalizeDouble(longSig?base+rr*slDist:base-rr*slDist,digits) : 0.0;
         ok=longSig? g_trade.Buy(lots,sym,price,sl,tp,"MIKAEL_DONCHIAN")
                   : g_trade.Sell(lots,sym,price,sl,tp,"MIKAEL_DONCHIAN");
      }
   }
   Print("[LIVE] ",sym," ",(longSig?"Buy":"Sell")," lots=",lots," -> ",
         ok?"OK":IntegerToString(g_trade.ResultRetcode()));
   LogRow(row+DoubleToString(lots,2)+";0;"+(ok?"FILLED":"REJ_"+IntegerToString(g_trade.ResultRetcode())));
   return true;
}
//+------------------------------------------------------------------+
int OnInit()
{
   if(ParseSymbols(InpSymbols)<=0)
   { Print("Aucune paire valide dans InpSymbols='",InpSymbols,"' — init annulee."); return INIT_FAILED; }
   ArrayResize(g_lastBar,g_nsym);
   ArrayResize(g_pendActive,g_nsym);
   ArrayResize(g_pendLong,g_nsym);
   ArrayResize(g_pendSlDist,g_nsym);
   ArrayResize(g_pendExpiry,g_nsym);
   ArrayResize(g_pendRefPx,g_nsym);
   ArrayResize(g_coolUntil,g_nsym);

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(20);
   string acc=IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string pfx="MIKAEL_DC_"+IntegerToString(InpMagic)+"_"+acc+"_";
   g_gvPeak=pfx+"peak";
   g_gvHalt=pfx+"halt";
   g_gvDayA=pfx+"dayanchor";
   g_gvDayD=pfx+"dayofyear";
   g_gvCool=pfx+"cool_";
   for(int i=0;i<g_nsym;i++){
      g_lastBar[i]=0; g_pendActive[i]=false;
      g_coolUntil[i]=GlobalVariableCheck(g_gvCool+SYMBOLS[i])?
                     (datetime)(long)GlobalVariableGet(g_gvCool+SYMBOLS[i]) : 0;
   }
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   g_peak=GlobalVariableCheck(g_gvPeak)? MathMax(GlobalVariableGet(g_gvPeak),eq) : eq;
   GlobalVariableSet(g_gvPeak,g_peak);
   g_halted=(GlobalVariableCheck(g_gvHalt) && GlobalVariableGet(g_gvHalt)>0.5);
   if(g_halted) Print("!! HALT persistant actif — aucune entree. ",
                      "Supprimer la variable globale ",g_gvHalt," pour re-armer.");

   int ftmoDay=FtmoDayOfYear();
   if(GlobalVariableCheck(g_gvDayD) && (int)GlobalVariableGet(g_gvDayD)==ftmoDay
      && GlobalVariableCheck(g_gvDayA)){
      g_dayAnchor=GlobalVariableGet(g_gvDayA);
      g_dayOfYear=ftmoDay;
      Print("Ancre journaliere restauree: ",DoubleToString(g_dayAnchor,2));
   }else{
      g_dayAnchor=AccountInfoDouble(ACCOUNT_BALANCE);
      g_dayOfYear=ftmoDay;
      GlobalVariableSet(g_gvDayA,g_dayAnchor);
      GlobalVariableSet(g_gvDayD,g_dayOfYear);
   }

   // cooldown retroactif (pertes fermees pendant que l'EA etait eteint)
   if(InpCooldownHours>0 && HistorySelect(TimeCurrent()-(long)InpCooldownHours*3600,TimeCurrent())){
      for(int h=HistoryDealsTotal()-1;h>=0;h--){
         ulong dl=HistoryDealGetTicket(h);
         if(HistoryDealGetInteger(dl,DEAL_MAGIC)!=InpMagic) continue;
         if(HistoryDealGetInteger(dl,DEAL_ENTRY)!=DEAL_ENTRY_OUT) continue;
         double pl=HistoryDealGetDouble(dl,DEAL_PROFIT)
                  +HistoryDealGetDouble(dl,DEAL_SWAP)
                  +HistoryDealGetDouble(dl,DEAL_COMMISSION);
         if(pl>=0) continue;
         int ci=SymIndex(HistoryDealGetString(dl,DEAL_SYMBOL)); if(ci<0) continue;
         datetime until=(datetime)HistoryDealGetInteger(dl,DEAL_TIME)+(datetime)InpCooldownHours*3600;
         if(until>g_coolUntil[ci]){
            g_coolUntil[ci]=until;
            GlobalVariableSet(g_gvCool+SYMBOLS[ci],(double)(long)until);
            Print("[COOLDOWN retroactif] ",SYMBOLS[ci]," gele jusqu'a ",TimeToString(until));
         }
      }
   }

   g_fileLog=FileOpen("MIKAEL_DONCHIAN_journal.csv",FILE_READ|FILE_WRITE|FILE_SHARE_READ|FILE_TXT|FILE_ANSI);
   if(g_fileLog==INVALID_HANDLE)
      Print("!! journal CSV indisponible (err ",GetLastError(),") — l'EA continue sans log fichier");
   else if(FileSize(g_fileLog)==0)
      LogRow("time;symbol;dir;adx;signal;price;lots;dry;note");
   EventSetTimer(30);
   string symList=""; for(int i=0;i<g_nsym;i++) symList+=(i>0?",":"")+SYMBOLS[i];
   string stratStr;
   if(InpStrategy==STRAT_SCALP)
      stratStr="SCALP (pullback EMA"+IntegerToString(InpScalpEMAfast)+"/"+IntegerToString(InpScalpEMAslow)+
               ", SL="+DoubleToString(InpScalpSLATR,1)+"xATR, RR="+DoubleToString(InpScalpRR,2)+
               ", RSIfloor="+IntegerToString(InpScalpRSIfloor)+")";
   else
      stratStr="DONCHIAN (canal "+IntegerToString(InpEntryPeriod)+"/"+IntegerToString(InpExitPeriod)+
               ", SL_mode="+EnumToString(InpSLMode)+", S&R="+(InpStopAndReverse?"ON":"OFF")+
               ", RR="+DoubleToString(InpRR,2)+(InpRR<=0?" [sortie canal]":"")+")";
   string riskStr=(InpRiskCashFixed>0)? DoubleToString(InpRiskCashFixed,2)+"$/trade (FIXE)"
                                      : DoubleToString(InpRiskPerTrade*100,2)+"% equity";
   Print("MIKAEL_DONCHIAN v1.14 init OK | sent_filter=",
         (InpSentThreshold>0?"ON (seuil "+DoubleToString(InpSentThreshold,2)+", fail-open)":"OFF"),
         " | ",stratStr," | paires=",symList," (",g_nsym,") | TF=",EnumToString(InpSignalTF),
         " | filtres: EMA",InpEMAPeriod," D1_SMA",InpTrendMAD1," ADX>=",DoubleToString(InpMinADX,0),
         " RSIcap=",DoubleToString(InpRSICap,0),
         " | risk=",riskStr," | daily=",
         DoubleToString(InpDailyStopPct*100,1),"% | dry_run=",InpDryRun,
         (InpDryRun?" ⚠️ AUCUN ORDRE REEL (validation)":" !! ORDRES REELS !!"));
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_fileLog!=INVALID_HANDLE) FileClose(g_fileLog);
}
//+------------------------------------------------------------------+
//| Sortie de canal : ferme la position si la cloture franchit le    |
//| canal oppose InpExitPeriod. Retourne +1/-1 = direction du REVERSE|
//| souhaite (0 = pas de reverse). Tourne MEME en halt (gestion).    |
//+------------------------------------------------------------------+
int ManageChannelExit(const string sym, const Indi &v)
{
   if(!SymbolBusy(sym)) return 0;
   for(int i=PositionsTotal()-1;i>=0;i--){
      if(PositionGetSymbol(i)!=sym) continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      bool isLong=(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
      bool exitSig=(isLong && v.close<v.loX) || (!isLong && v.close>v.hiX);
      if(!exitSig) return 0;
      ulong ticket=PositionGetInteger(POSITION_TICKET);
      if(InpDryRun){
         Print("[DRY][EXIT-CANAL] ",sym," ",(isLong?"long":"short")," close=",v.close,
               " a franchi le canal ",InpExitPeriod);
         return InpStopAndReverse ? (isLong?-1:1) : 0;
      }
      if(g_trade.PositionClose(ticket)){
         Print("[EXIT-CANAL] ",sym," ",(isLong?"long":"short")," fermee (canal ",InpExitPeriod,
               " franchi a ",v.close,")");
         LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(isLong?"Buy":"Sell")+
                ";0;0;"+DoubleToString(v.close,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS))+
                ";0;0;EXIT_CANAL");
         return InpStopAndReverse ? (isLong?-1:1) : 0;
      }
      Print("[EXIT-CANAL] echec fermeture ",sym," ret=",g_trade.ResultRetcode());
      return 0;
   }
   return 0;
}
//+------------------------------------------------------------------+
//| SCALP (v1.14) : sortie quand le BIAIS meurt — long ferme si       |
//| EMA rapide repasse sous la lente (et inversement). C'est la       |
//| sortie « signal » institutionnelle ; le TP fixe devient optionnel.|
//+------------------------------------------------------------------+
void ManageScalpBiasExit(const string sym, const Indi &v)
{
   for(int i=PositionsTotal()-1;i>=0;i--){
      if(PositionGetSymbol(i)!=sym) continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      bool isLong=(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
      bool exitSig=(isLong && v.emaF<v.emaS) || (!isLong && v.emaF>v.emaS);
      if(!exitSig) return;
      ulong ticket=PositionGetInteger(POSITION_TICKET);
      if(InpDryRun) return;
      if(g_trade.PositionClose(ticket)){
         Print("[EXIT-BIAIS] ",sym," ",(isLong?"long":"short"),
               " fermee — biais EMA",InpScalpEMAfast,"/",InpScalpEMAslow," inverse");
         LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(isLong?"Buy":"Sell")+
                ";0;0;"+DoubleToString(v.close,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS))+
                ";0;0;EXIT_BIAIS");
      }else
         Print("[EXIT-BIAIS] echec fermeture ",sym," ret=",g_trade.ResultRetcode());
      return;
   }
}
//+------------------------------------------------------------------+
void OnTimer()
{
   EnforceTimeStop();

   // --- ancre journaliere + kill switches (copie MIKAEL_IA) ---
   MqlDateTime now; TimeToStruct(TimeCurrent(),now);
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   int ftmoDay=FtmoDayOfYear();
   if(ftmoDay!=g_dayOfYear){
      g_dayOfYear=ftmoDay; g_dayAnchor=AccountInfoDouble(ACCOUNT_BALANCE);
      GlobalVariableSet(g_gvDayA,g_dayAnchor); GlobalVariableSet(g_gvDayD,g_dayOfYear);
   }
   g_peak=MathMax(g_peak,eq);
   GlobalVariableSet(g_gvPeak,g_peak);
   if(g_halted) GlobalVariableSet(g_gvHalt,1.0);
   double dayPl=(g_dayAnchor>0)?(eq-g_dayAnchor)/g_dayAnchor:0;
   double ddRef=(InpInitialBalance>0)?InpInitialBalance:g_peak;
   double dd=(ddRef>0)?(ddRef-eq)/ddRef:0;
   if(!g_halted && dd>=InpMaxDDPct){
      g_halted=true; GlobalVariableSet(g_gvHalt,1.0);
      Print("!! KILL SWITCH perte totale ",DoubleToString(dd*100,1),
            "% sous ",(InpInitialBalance>0?"le solde initial":"le pic"),
            " — entrees stoppees DEFINITIVEMENT (positions restantes gerees)");
   }
   static bool targetLogged=false;
   bool targetHit=(InpInitialBalance>0 && InpTargetPct>0 &&
                   AccountInfoDouble(ACCOUNT_BALANCE)>=InpInitialBalance*(1.0+InpTargetPct));
   if(targetHit && !targetLogged){
      targetLogged=true;
      Print("== OBJECTIF DE PROFIT ATTEINT (",DoubleToString(InpTargetPct*100,1),
            "%) — entrees stoppees ==");
   }
   bool halt = g_halted || (dayPl<=-InpDailyStopPct) || targetHit;

   bool canTrade = !InpDryRun
                && (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
                && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED)
                && (bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED);

   for(int s=0;s<g_nsym;s++){
      string sym=SYMBOLS[s];

      // --- 1) signal en attente (spread trop large) : retry ---
      if(g_pendActive[s]){
         if(TimeCurrent()>=g_pendExpiry[s]){
            g_pendActive[s]=false;
            LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(g_pendLong[s]?"Buy":"Sell")+
                   ";0;1;0;0;"+(InpDryRun?"1":"0")+";pend_timeout");
         }
         else if(!halt && !(now.day_of_week==5 && now.hour>=InpNoFridayAfter)){
            if(TryEnter(sym,g_pendLong[s],g_pendSlDist[s],0,canTrade,g_pendRefPx[s]))
               g_pendActive[s]=false;
         }
      }

      // --- 2) nouvelle bougie fermee sur le TF de signal ---
      datetime bt=iTime(sym,InpSignalTF,1);
      if(bt==0 || bt==g_lastBar[s]) continue;
      g_lastBar[s]=bt;
      g_pendActive[s]=false;                  // signal non execute perime a la bougie suivante
      if(SymbolInfoInteger(sym,SYMBOL_TRADE_MODE)!=SYMBOL_TRADE_MODE_FULL) continue;

      // echec CopyRates = transitoire (VPS resync) : rendre la bougie, retry 30s
      MqlRates r[]; ArraySetAsSeries(r,false);
      if(CopyRates(sym,InpSignalTF,1,LOOKBACK,r)<MathMax(InpEMAPeriod+10,60)){ g_lastBar[s]=0; continue; }
      MqlRates rD[]; ArraySetAsSeries(rD,false);
      if(CopyRates(sym,PERIOD_D1,1,MathMax(InpTrendMAD1+20,60),rD)<MathMin(InpTrendMAD1,55)){ g_lastBar[s]=0; continue; }

      Indi v;
      if(!ComputeIndi(r,v)) continue;

      bool friday=(now.day_of_week==5 && now.hour>=InpNoFridayAfter);

      // --- 3) gestion des sorties par le SIGNAL ---
      // DONCHIAN : sortie de canal + stop&reverse. SCALP : sortie au flip du
      // biais EMA (v1.14) — le SL catastrophe et le time-stop restent en garde.
      if(InpStrategy==STRAT_SCALP) ManageScalpBiasExit(sym,v);
      if(InpStrategy==STRAT_DONCHIAN){
         int rev=ManageChannelExit(sym,v);
         if(rev!=0 && !halt && !friday){
            bool longR=(rev>0);
            string whyR="";
            if(!FiltersAllow(sym,longR,rD,v,whyR)){
               LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(longR?"Buy":"Sell")+
                      ";"+DoubleToString(v.adx,1)+";0;0;0;"+(InpDryRun?"1":"0")+";reverse_"+whyR);
               Print("[REVERSE] ",sym," ",(longR?"Buy":"Sell")," refuse: ",whyR);
            }else{
               double slR=SlDistance(longR,v);
               if(!TryEnter(sym,longR,slR,v.adx,canTrade,v.close)){
                  g_pendActive[s]=true; g_pendLong[s]=longR; g_pendSlDist[s]=slR;
                  g_pendRefPx[s]=v.close;
                  g_pendExpiry[s]=TimeCurrent()+90*60;
                  Print("[WAIT] ",sym," reverse en attente (spread)");
               }
            }
            continue; // pas de double signal sur la meme bougie
         }
      }

      // --- 4) entree (commun aux deux moteurs) ---
      if(halt || friday) continue;
      if(SymbolBusy(sym)) continue;

      bool longSig=false; double slDist=0.0; string why="";
      bool haveSignal=false;

      if(InpStrategy==STRAT_DONCHIAN){
         bool breakUp=(v.close>v.hiE), breakDn=(v.close<v.loE);
         if(!breakUp && !breakDn) continue;
         longSig=breakUp; haveSignal=true;
         slDist=SlDistance(longSig,v);
      }else{ // STRAT_SCALP
         int sig=ScalpSignal(v,why);
         if(sig==0){
            if(why!="") LogRow(TimeToString(TimeCurrent())+";"+sym+";?;"+
                               DoubleToString(v.adx,1)+";0;0;0;"+(InpDryRun?"1":"0")+";"+why);
            continue;
         }
         longSig=(sig>0); haveSignal=true;
         slDist=MathMax(InpScalpSLATR*v.atr,0.5*v.atr); // stop serre, plancher 0.5 ATR
      }
      if(!haveSignal) continue;

      // filtres de tendance/force communs (EMA200/D1/ADX/RSI-cap)
      if(!FiltersAllow(sym,longSig,rD,v,why)){
         LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(longSig?"Buy":"Sell")+
                ";"+DoubleToString(v.adx,1)+";0;0;0;"+(InpDryRun?"1":"0")+";"+why);
         Print("[FILTRE] ",sym," ",(longSig?"Buy":"Sell")," refuse: ",why,
               " (adx=",DoubleToString(v.adx,1)," rsi=",DoubleToString(v.rsi,1),")");
         continue;
      }

      if(!TryEnter(sym,longSig,slDist,v.adx,canTrade,v.close)){
         g_pendActive[s]=true; g_pendLong[s]=longSig; g_pendSlDist[s]=slDist;
         g_pendRefPx[s]=v.close;
         g_pendExpiry[s]=TimeCurrent()+90*60;
         Print("[WAIT] ",sym," spread trop large — signal en attente (retry 30s, max 90 min)");
      }
   }
}
//+------------------------------------------------------------------+
//| Cooldown apres perte (actif seulement si InpCooldownHours>0)     |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(InpCooldownHours<=0) return;
   if(trans.type!=TRADE_TRANSACTION_DEAL_ADD) return;
   if(!HistoryDealSelect(trans.deal)) return;
   if(HistoryDealGetInteger(trans.deal,DEAL_MAGIC)!=InpMagic) return;
   if(HistoryDealGetInteger(trans.deal,DEAL_ENTRY)!=DEAL_ENTRY_OUT) return;
   double pl=HistoryDealGetDouble(trans.deal,DEAL_PROFIT)
            +HistoryDealGetDouble(trans.deal,DEAL_SWAP)
            +HistoryDealGetDouble(trans.deal,DEAL_COMMISSION);
   if(pl>=0) return;
   string sym=HistoryDealGetString(trans.deal,DEAL_SYMBOL);
   int idx=SymIndex(sym); if(idx<0) return;
   g_coolUntil[idx]=TimeCurrent()+(datetime)InpCooldownHours*3600;
   GlobalVariableSet(g_gvCool+sym,(double)(long)g_coolUntil[idx]);
   g_pendActive[idx]=false;
   Print("[COOLDOWN] ",sym," fermee en perte (",DoubleToString(pl,2),
         ") — pas de re-entree avant ",TimeToString(g_coolUntil[idx]));
}
//+------------------------------------------------------------------+
void OnTick() { /* logique pilotee par OnTimer (30s) */ }
//+------------------------------------------------------------------+
