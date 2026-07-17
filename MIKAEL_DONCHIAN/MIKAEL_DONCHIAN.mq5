//+------------------------------------------------------------------+
//| MIKAEL_DONCHIAN.mq5 — moteur UNIQUE : CANDLE, armature FTMO      |
//| v2.10 : les moteurs Donchian (Turtle) et Scalp (pullback EMA)   |
//|  ont ete RETIRES. Il ne reste que le moteur CANDLE (patterns de |
//|  bougies) + martingale plafonnee + breakeven/trailing ATR. Le   |
//|  nom de fichier, le magic et les prefixes de variables globales |
//|  sont conserves a l'identique pour ne pas casser la continuite  |
//|  de l'instance en forward-test sur le VPS.                       |
//|                                                                  |
//| MOTEUR CANDLE :                                                  |
//|  ENTREE  : pattern de bougie sur la derniere bougie FERMEE du TF |
//|            de signal (englobante bull/bear ou pin bar), corps    |
//|            minimal InpCdlBodyATRmin x ATR (anti-doji)            |
//|  STOP    : extreme du pattern +/- InpCdlSLBufATR x ATR, plancher |
//|            InpCdlSLATRFloor x ATR (anti-lot-enorme)             |
//|  TP      : InpCdlRR x distance SL (>=1 requis pour que la        |
//|            martingale recupere les pertes)                       |
//|  GESTION : breakeven + trailing ATR (un gain ne redevient jamais |
//|            une perte) + martingale plafonnee par serie de pertes |
//|                                                                  |
//|  FILTRES communs (couche partagee) :                             |
//|   - EMA InpEMAPeriod (200) sur le TF de signal : long ssi        |
//|     close>EMA, short ssi close<EMA (0=off)                       |
//|   - SMA InpTrendMAD1 sur D1 : tendance de fond (0=off)           |
//|   - ADX14 >= InpMinADX : force de tendance requise (0=off)       |
//|   - RSI14 : cap surachat/survente (InpRSICap=100 -> off)         |
//|   - sentiment macro (macro_features.csv) : veto anti-flux (fail-open) |
//|                                                                  |
//| MARTINGALE PLAFONNEE : multiplicateur par serie de pertes PAR    |
//|  PAIRE, plafond en pas ET en % du capital, bride par le budget   |
//|  journalier prospectif FTMO deja en place.                       |
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
//|  - cooldown optionnel apres perte (defaut 0)                     |
//|  - time-stop optionnel (defaut 0)                                |
//|  - fill type par symbole (anti-10030), retry requote, stops level|
//|  NOTE : pas de module day-ticket (FTMO a supprime les jours mini;|
//|  sur compte FINANCE, re-attacher MIKAEL_IA ou ajouter le module) |
//|  Instance recommandee : magic 20260720 (JAMAIS un magic adjacent |
//|  a un autre EA : le day-ticket d'IA/MACRO vise magic+1).         |
//+------------------------------------------------------------------+
#property copyright "Mbula"
#property version   "2.10"
#property strict

#include <Trade\Trade.mqh>

//--- INPUTS strategie (moteur unique : CANDLE)
input ENUM_TIMEFRAMES InpSignalTF   = PERIOD_H1; // timeframe des signaux (bougie fermee)
//--- FILTRES anti-fausse-cassure
input int    InpEMAPeriod     = 200;       // EMA tendance sur TF de signal (0=off) — SEUL filtre de tendance par defaut
input int    InpTrendMAD1     = 0;         // SMA D1 tendance de fond (0=off) — desactive : double filtre trop strict (etouffait les cassures)
input double InpMinADX        = 15.0;      // ADX14 minimum (0=off) — 15 laisse passer les cassures naissantes (20 = trop tardif sur FX)
input double InpRSICap        = 100.0;     // pas d'achat si RSI>cap, pas de vente si RSI<100-cap (100=OFF). ⚠️ un cap<100 CONTREDIT la cassure : un plus-haut 20 barres a par nature un RSI eleve — le RSI rejetait les meilleures tendances
//--- FILTRE SENTIMENT/MACRO (macro_features.csv ecrit par v4_macro\macro_service.py)
input double InpSentThreshold = 0.15;      // veto : pas de Buy si sent(base)-sent(quote) < -seuil, pas de Sell si > +seuil. 0=off
input int    InpSentMaxAgeH   = 12;        // fraicheur max du fichier (h) ; perime/absent = filtre INACTIF (log) — jamais bloquant
//--- PARAMETRES CANDLE
input bool   InpCdlEngulfing  = true;      // pattern englobante (bull/bear engulfing)
input bool   InpCdlPinbar     = true;      // pattern pin bar (marteau / etoile filante)
input double InpCdlBodyATRmin = 0.25;      // corps minimal du signal en xATR (anti-doji/bruit)
input double InpCdlSLBufATR   = 0.10;      // marge du SL au-dela de l'extreme du pattern (xATR)
input double InpCdlSLATRFloor = 0.80;      // plancher du SL en xATR (anti-lot-enorme, meme esprit que 0.5xATR Donchian)
input double InpCdlRR         = 1.5;       // TP = RR x SL. >=1 REQUIS pour que la martingale recupere les pertes
//--- MARTINGALE PLAFONNEE (record de pertes PAR PAIRE ; 1 seule position/paire)
input bool   InpMartEnable    = true;      // x le risque apres chaque perte, reset au 1er gain/BE
input double InpMartMult      = 2.0;       // multiplicateur par perte consecutive
input int    InpMartMaxSteps  = 2;         // pas max : risque plafonne a base x Mult^MaxSteps (2 -> x4 max)
input double InpMartMaxRiskPct= 0.015;     // cap ABSOLU du risque d'un trade en % equity (garde FTMO)
input int    InpMartLookbackD = 14;        // profondeur d'historique (jours) pour compter la serie de pertes
//--- BREAKEVEN + TRAILING STOP (gestion ATR, tous moteurs ; 0 = off)
input double InpBETriggerATR  = 1.0;       // profit >= x*ATR -> SL remonte a l'entree (+buffer) : le trade ne peut plus perdre
input double InpBEBufferATR   = 0.05;      // buffer du breakeven (xATR) — couvre spread+commission
input double InpTrailStartATR = 1.5;       // profit >= x*ATR -> trailing actif
input double InpTrailATR      = 1.2;       // distance du trailing stop (xATR), ne fait que se RESSERRER
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
input double InpInitialBalance = 100000;   // solde initial FTMO : ref STATIQUE Max Loss + target ; 0 = repli peak — DOIT = taille reelle du compte
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
double   g_lastAtr[];      // ATR14 TF signal par paire (rafraichi a chaque bougie) — trailing/BE

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
//+------------------------------------------------------------------+
struct Indi
{
   double close;                  // cloture de la bougie de signal
   double ema;                    // EMA tendance (0 si off)
   double atr, rsi, adx;
};
bool ComputeIndi(const MqlRates &r[], Indi &v)
{
   int n=ArraySize(r);
   int need=MathMax(InpEMAPeriod+10,60);
   if(n<need) return false;
   int i=n-1;                      // derniere bougie fermee
   v.close=r[i].close;

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
//| CANDLE : patterns de bougies sur la derniere bougie fermee.      |
//| Englobante : le corps de la bougie signal AVALE le corps de la   |
//| precedente, dans le sens oppose. Pin bar : meche dominante >=60% |
//| du range, corps <=30%, cloture dans le tiers oppose a la meche.  |
//| SL = extreme du pattern +/- buffer, plancher InpCdlSLATRFloor.   |
//| Retourne +1/-1/0 ; les filtres communs (EMA200/ADX/sentiment)    |
//| s'appliquent ensuite via FiltersAllow.                           |
//+------------------------------------------------------------------+
int CandleSignal(const MqlRates &r[], const Indi &v, double &slDist, string &pat)
{
   int n=ArraySize(r); if(n<3) return 0;
   int i=n-1, p=n-2;
   double o1=r[i].open, c1=r[i].close, h1=r[i].high, l1=r[i].low;
   double o0=r[p].open, c0=r[p].close;
   double body1=MathAbs(c1-o1), body0=MathAbs(c0-o0);
   double range1=h1-l1;
   double atr=v.atr;
   pat=""; slDist=0.0;
   if(atr<=0 || range1<=0) return 0;

   int sig=0;
   if(InpCdlEngulfing && body1>=InpCdlBodyATRmin*atr && body1>=body0){
      // englobante haussiere : bougie prec baissiere, signal haussier qui l'avale
      if(c1>o1 && c0<o0 && c1>=o0 && o1<=c0){ sig=+1; pat="engulf_bull"; }
      // englobante baissiere : symetrique
      else if(c1<o1 && c0>o0 && c1<=o0 && o1>=c0){ sig=-1; pat="engulf_bear"; }
   }
   if(sig==0 && InpCdlPinbar && range1>=0.8*atr && body1<=0.30*range1){
      double upWick=h1-MathMax(o1,c1);
      double dnWick=MathMin(o1,c1)-l1;
      // marteau : longue meche basse (rejet des vendeurs), cloture dans le tiers haut
      if(dnWick>=0.60*range1 && c1>=l1+0.66*range1){ sig=+1; pat="pin_hammer"; }
      // etoile filante : symetrique
      else if(upWick>=0.60*range1 && c1<=h1-0.66*range1){ sig=-1; pat="pin_star"; }
   }
   if(sig==0) return 0;

   // SL au-dela de l'extreme du pattern, plancher anti-lot-enorme
   double d=(sig>0)? (v.close-l1) : (h1-v.close);
   slDist=MathMax(d+InpCdlSLBufATR*atr, InpCdlSLATRFloor*atr);
   return sig;
}
//+------------------------------------------------------------------+
//| MARTINGALE PLAFONNEE : multiplicateur = Mult^(pertes consecutives|
//| PAR PAIRE, magic strict), plafonne en pas ET en % du capital.    |
//| Serie relue depuis l'historique des deals (robuste aux restarts, |
//| aucun etat a persister). Un BE/petit scratch (>-20% du risque de |
//| base) NE compte PAS comme une perte (sinon le trailing BE ferait |
//| doubler a tort). Le budget journalier prospectif (TryEnter) et le|
//| kill switch FTMO restent au-dessus de tout.                      |
//+------------------------------------------------------------------+
double MartRiskMultiplier(const string sym)
{
   if(!InpMartEnable || InpMartMaxSteps<=0 || InpMartMult<=1.0) return 1.0;
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   double baseRisk=(InpRiskCashFixed>0)? InpRiskCashFixed : eq*InpRiskPerTrade;
   if(baseRisk<=0) return 1.0;
   double lossThr=0.20*baseRisk;   // en-deca : scratch/BE, pas une vraie perte

   int streak=0;
   if(HistorySelect(TimeCurrent()-(long)InpMartLookbackD*86400,TimeCurrent())){
      for(int h=HistoryDealsTotal()-1;h>=0 && streak<InpMartMaxSteps;h--){
         ulong dl=HistoryDealGetTicket(h);
         if(HistoryDealGetInteger(dl,DEAL_MAGIC)!=InpMagic) continue;
         if(HistoryDealGetString(dl,DEAL_SYMBOL)!=sym) continue;
         if(HistoryDealGetInteger(dl,DEAL_ENTRY)!=DEAL_ENTRY_OUT) continue;
         double pl=HistoryDealGetDouble(dl,DEAL_PROFIT)
                  +HistoryDealGetDouble(dl,DEAL_SWAP)
                  +HistoryDealGetDouble(dl,DEAL_COMMISSION);
         if(pl<-lossThr) streak++;      // vraie perte : on monte d'un pas
         else break;                    // gain ou scratch : serie terminee
      }
   }
   if(streak<=0) return 1.0;
   double mult=MathPow(InpMartMult,streak);
   // cap absolu : le risque du trade ne depasse JAMAIS InpMartMaxRiskPct de l'equity
   double capMult=(eq*InpMartMaxRiskPct)/baseRisk;
   if(mult>capMult) mult=capMult;
   return MathMax(mult,1.0);
}
//+------------------------------------------------------------------+
//| BREAKEVEN + TRAILING (tous moteurs, toutes les 30 s, meme en     |
//| halt : c'est de la GESTION, pas une entree).                     |
//|  1) profit >= BETrigger x ATR  -> SL = entree +/- buffer          |
//|  2) profit >= TrailStart x ATR -> SL = prix -/+ TrailATR x ATR    |
//| Le SL ne fait QUE se resserrer — un gain ne redevient jamais     |
//| une perte (demande utilisateur, regle "non-perte").              |
//+------------------------------------------------------------------+
void ManageBreakevenTrailing()
{
   if(InpBETriggerATR<=0 && InpTrailATR<=0) return;
   for(int i=PositionsTotal()-1;i>=0;i--){
      string sym=PositionGetSymbol(i);
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      int s=SymIndex(sym); if(s<0) continue;
      double atr=g_lastAtr[s]; if(atr<=0) continue;

      bool   isLong=(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
      double open=PositionGetDouble(POSITION_PRICE_OPEN);
      double cur =PositionGetDouble(POSITION_PRICE_CURRENT);
      double sl  =PositionGetDouble(POSITION_SL);
      double tp  =PositionGetDouble(POSITION_TP);
      double prof=isLong? (cur-open) : (open-cur);
      int    dg  =(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
      double pt  =SymbolInfoDouble(sym,SYMBOL_POINT);

      double newSl=sl;
      if(InpBETriggerATR>0 && prof>=InpBETriggerATR*atr){
         double be=isLong? open+InpBEBufferATR*atr : open-InpBEBufferATR*atr;
         if(sl<=0 || (isLong? be>newSl : be<newSl)) newSl=be;
      }
      if(InpTrailATR>0 && prof>=InpTrailStartATR*atr){
         double tr=isLong? cur-InpTrailATR*atr : cur+InpTrailATR*atr;
         if(newSl<=0 || (isLong? tr>newSl : tr<newSl)) newSl=tr;
      }
      newSl=NormalizeDouble(newSl,dg);
      if(newSl==sl || newSl<=0) continue;
      if(isLong? (sl>0 && newSl<=sl+pt) : (sl>0 && newSl>=sl-pt)) continue; // resserrement uniquement

      // distance minimale broker
      double minDist=SymbolInfoInteger(sym,SYMBOL_TRADE_STOPS_LEVEL)*pt;
      if(MathAbs(cur-newSl)<minDist) continue;

      ulong ticket=PositionGetInteger(POSITION_TICKET);
      if(g_trade.PositionModify(ticket,newSl,tp))
         Print("[TRAIL] ",sym," SL -> ",DoubleToString(newSl,dg),
               (prof>=InpTrailStartATR*atr?" (trailing)":" (breakeven)"));
   }
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
      return (datetime)(TimeCurrent()-(long)InpDayResetOffsetH*3600);
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
   // CANDLE : TP = InpCdlRR x SL (>=1 requis pour la recuperation martingale)
   double rr=InpCdlRR;
   int digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
   double base=longSig?tick.bid:tick.ask;
   double sl=NormalizeDouble(longSig?base-slDist:base+slDist,digits);
   double tp=(rr>0)? NormalizeDouble(longSig?base+rr*slDist:base-rr*slDist,digits) : 0.0;
   if(!StopsValid(sym,price,sl,tp)){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";stops_level"); return true; }

   // sizing exact — risque en $ FIXE (InpRiskCashFixed>0) sinon % de l'equity,
   // multiplie par la martingale plafonnee (1.0 si off / serie vierge)
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   string why="";
   double mart=MartRiskMultiplier(sym);
   double riskCash=((InpRiskCashFixed>0)? InpRiskCashFixed : eq*InpRiskPerTrade)*mart;
   if(mart>1.0) Print("[MART] ",sym," serie de pertes -> risque x",DoubleToString(mart,2),
                      " (",DoubleToString(riskCash,2),"$)");
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
   ArrayResize(g_lastAtr,g_nsym);
   ArrayInitialize(g_lastAtr,0.0);

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

   // journal PAR INSTANCE (suffixe magic) : deux EA qui partageraient le meme
   // fichier -> la 2e echoue en err 5004
   g_fileLog=FileOpen("MIKAEL_DONCHIAN_journal_"+IntegerToString(InpMagic)+".csv",
                      FILE_READ|FILE_WRITE|FILE_SHARE_READ|FILE_TXT|FILE_ANSI);
   if(g_fileLog==INVALID_HANDLE)
      Print("!! journal CSV indisponible (err ",GetLastError(),") — l'EA continue sans log fichier");
   else if(FileSize(g_fileLog)==0)
      LogRow("time;symbol;dir;adx;signal;price;lots;dry;note");
   EventSetTimer(30);
   string symList=""; for(int i=0;i<g_nsym;i++) symList+=(i>0?",":"")+SYMBOLS[i];
   string stratStr="CANDLE (engulf="+(InpCdlEngulfing?"ON":"OFF")+" pin="+(InpCdlPinbar?"ON":"OFF")+
               ", RR="+DoubleToString(InpCdlRR,2)+
               ", MART="+(InpMartEnable?"x"+DoubleToString(InpMartMult,1)+"^"+IntegerToString(InpMartMaxSteps)+
               " cap "+DoubleToString(InpMartMaxRiskPct*100,1)+"%":"OFF")+
               ", BE@"+DoubleToString(InpBETriggerATR,1)+"ATR trail@"+DoubleToString(InpTrailStartATR,1)+
               "/"+DoubleToString(InpTrailATR,1)+"ATR)";
   string riskStr=(InpRiskCashFixed>0)? DoubleToString(InpRiskCashFixed,2)+"$/trade (FIXE)"
                                      : DoubleToString(InpRiskPerTrade*100,2)+"% equity";
   Print("MIKAEL_DONCHIAN v2.10 (CANDLE only) init OK | sent_filter=",
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
void OnTimer()
{
   EnforceTimeStop();
   ManageBreakevenTrailing();   // BE + trailing a chaque cycle (gestion, tourne meme en halt)

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
      g_lastAtr[s]=v.atr;               // cache ATR pour le trailing inter-bougies

      bool friday=(now.day_of_week==5 && now.hour>=InpNoFridayAfter);

      // --- 3) entree CANDLE ---
      // (pas de sortie par signal : SL/TP fixes + breakeven/trailing + time-stop)
      if(halt || friday) continue;
      if(SymbolBusy(sym)) continue;

      bool longSig=false; double slDist=0.0; string why="";

      string pat="";
      int sig=CandleSignal(r,v,slDist,pat);
      if(sig==0) continue;
      longSig=(sig>0);
      Print("[CANDLE] ",sym," pattern ",pat," ",(longSig?"Buy":"Sell"),
            " sl_dist=",DoubleToString(slDist/SymbolInfoDouble(sym,SYMBOL_POINT),0),"pt");

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
