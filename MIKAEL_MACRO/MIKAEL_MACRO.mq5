//+------------------------------------------------------------------+
//| MIKAEL_MACRO.mq5 — EA V4 : modele macro (calendrier+FRED) + veto |
//| sentiment FinBERT. Chassis FTMO herite de MIKAEL_IA v1.79.       |
//|                                                                  |
//| ⚠️ VERDICT BACKTEST : NO-GO (R_net -0.038 apres couts, val 24-25;|
//| Spearman +0.05 reel mais insuffisant). CET EA EXISTE UNIQUEMENT  |
//| pour tester en FORWARD DEMO l'hypothese « modele + veto FinBERT »|
//| (in-testable en backtest : pas d'historique sentiment).          |
//| NE JAMAIS L'ATTACHER A UN COMPTE REEL sans GO forward documente. |
//|                                                                  |
//| Dependances : macro_service.py en boucle (macro_features.csv     |
//| frais < InpMacroMaxAgeH, sinon AUCUN trade) ; model.onnx V4      |
//| (regression, parite exacte verifiee) ; seuil |score| 0.3601.     |
//| Features (11) : rsi,atrn,trend200,emax,hour,dow,sym_c,cal_s24,   |
//| cal_s72,fred_dgs2,fred_curve — ordre = model_meta.json.          |
//| Le modele model.onnx est EMBARQUE dans le .ex5 a la compilation. |
//|                                                                  |
//| v1.79 — timeframe de signal en input (InpSignalTF, defaut H4) :   |
//|  - signaux/features calcules sur le TF choisi (H1, M30, H4...) ;   |
//|    la confirmation du filtre tendance (SMA50) suit le TF de signal |
//|  - ⚠️ le modele ONNX reste entraine sur H4 : sur TF inferieur les  |
//|    features (atr_norm, hour 0-23 vs {0,4,...}, ret5) sortent de la |
//|    distribution d'entrainement -> valider en dry-run/demo avant    |
//|    tout argent reel ; SL pips fixes par paire inchanges (calibres  |
//|    H4 : sur H1 le SL est proportionnellement plus large)           |
//| v1.78 — filtre tendance manuel + fix compilation v1.77 :          |
//|  - InpTrendFilter (defaut ON) : aucun trade contre-tendance —      |
//|    long ssi close>SMA200 D1 ET close>SMA50 H4 ; short ssi les deux |
//|    en-dessous ; horizons en desaccord = aucun trade (periodes en   |
//|    input : InpTrendMAD1/InpTrendMAH4, ajustables sans recompiler)  |
//|  - fix : NSYM residuel (supprime au refactor v1.77) -> g_nsym      |
//|    (le source v1.77 ne compilait plus)                             |
//| v1.77 — paires en input (InpSymbols CSV, arrays dynamiques)        |
//| v1.76 — EURUSD SEUL :                                              |
//|  - traite uniquement EURUSD (NSYM=1) : la robustesse multi-paires  |
//|    est conservee (arrays/loops dimensionnes par NSYM) mais focus   |
//|    sur la paire la plus liquide / spread le plus fin               |
//|    (les filtres microstructure testes ont ete RETIRES : trop       |
//|     selectifs — logique d'entree = celle de v1.75)                 |
//| v1.75 — parite pipeline (audit dataset) :                         |
//|  - bars_since_flip clippe a 300 (= clip du dataset corrige) ;     |
//|    NE DEPLOYER cette version qu'avec le model.onnx RE-ENTRAINE    |
//|    sur le dataset sans lookahead D1 (NB1 corrige) et le seuil OOS |
//|    de model_meta.json reporte dans InpThreshold                   |
//| v1.74 — audit pre-VPS :                                           |
//|  - bougie non consommee si CopyRates echoue (reconnexion VPS :    |
//|    l'historique pas encore synchronise ne perd plus le signal)    |
//|  - type de remplissage d'ordre par symbole (FOK/IOC selon serveur,|
//|    evite le rejet 10030 INVALID_FILL au premier ordre live)       |
//|  - halt persistant rafraichi (les GlobalVariables MT5 expirent    |
//|    apres 4 semaines sans acces)                                   |
//|  - garde division par zero sur le budget journalier               |
//| v1.73 — durcissement FTMO SWING (obligatoire : portage week-end) :|
//|  - Max Loss = niveau STATIQUE (solde initial - InpMaxDDPct), plus |
//|    un drawdown glissant : un +5%/-7% depuis un pic ne halte plus   |
//|    definitivement un compte pourtant sain (=> challenge finissable)|
//|    -> RENSEIGNER InpInitialBalance (= taille du compte, ex 10000)  |
//|  - regle FTMO gap-trading : aucune entree (strategie NI day-ticket)|
//|    a moins de 2h de la cloture hebdo -> InpNoFridayAfter=22        |
//|  - day-ticket : FTMO a supprime les jours de trading minimum ->    |
//|    InpEnsureDayTrade=false en challenge/verif ; true + EveryD=25   |
//|    uniquement sur compte finance (anti-inactivite 30j)             |
//|  - fix fenetre day-ticket en mode AUTO (offset -1 mal interprete)  |
//| v1.51 — garde anti-derive GENERALISEE (redemarrage VPS) :        |
//|  - reference = cloture de la bougie de signal ; toute entree     |
//|    (immediate, differee, ou re-evaluation apres reboot) est      |
//|    abandonnee si le prix a derive de plus de InpMaxDriftSL x SL  |
//| v1.50 — fermeture des limites residuelles de l'audit :           |
//|  - jour FTMO : reset du budget journalier a minuit CE(S)T via    |
//|    InpDayResetOffsetH (serveur EET -> offset 1h), plus minuit srv|
//|  - garde anti-derive : une entree differee (file d'attente) est  |
//|    abandonnee si le prix a derive de plus de 25% du SL depuis le |
//|    signal (reste dans la distribution d'entrainement)            |
//|  - halt journalier : les signaux ne sont plus perdus, ils sont   |
//|    mis en attente jusqu'a la fin de la bougie en cours           |
//|  - cooldown retroactif : au demarrage, l'historique recent est   |
//|    scanne pour geler les paires perdantes pendant l'arret        |
//| v1.40 — cooldown anti-whipsaw :                                  |
//|  - apres une fermeture en perte, la paire est gelee 8h (2 bougies|
//|    H4) : empeche de retaper un support/resistance en range       |
//|    (la validation bloquait 48h ; l'EA ne bloquait qu'en position)|
//| v1.30 — gestion fine des spreads :                               |
//|  - spread large a l'ouverture de bougie (rollover 0h/4h) : le    |
//|    signal est mis en ATTENTE et reessaye toutes les 30s pendant  |
//|    90 min au lieu d'etre perdu                                   |
//|  - SL/TP ancres sur le prix de SORTIE (bid pour long, ask pour   |
//|    short) : distances identiques a l'entrainement, le spread     |
//|    n'elargit plus le SL                                          |
//| v1.20 — stop journalier FTMO prospectif :                        |
//|  - le budget de perte du jour compte le risque FLOTTANT des      |
//|    positions ouvertes (pire cas : tous les SL touches) + le      |
//|    risque du nouveau trade, pas seulement l'equity fermee        |
//|  - defauts calibres FTMO 2-step (risque 0.5%, daily 3.5%, DD 7%) |
//|    -> pour le 1-step : risk=0.0035, daily=0.02, conc=3           |
//| v1.10 — durcissement production :                                |
//|  - sizing exact via tick value broker (plus d'approximation pip) |
//|  - time-stop 168h (aligne sur MAX_HOLD_H de l'entrainement)      |
//|  - filtre de spread par symbole                                  |
//|  - kill-switch persistant (GlobalVariables, survit au redemarrage)|
//|  - halt au lieu d'ExpertRemove (les positions restent gerees)    |
//|  - verif marge, stops level, retcode, retry sur requote          |
//|  - garde-fou lot minimum (refuse de sur-risquer un petit compte) |
//+------------------------------------------------------------------+
#property copyright "Mbula"
#property version   "2.13"
#property strict

#include <Trade\Trade.mqh>

//--- MODELE ONNX embarque (model.onnx doit etre dans le MEME dossier que ce .mq5)
#resource "model.onnx" as uchar ExtModel[]

//--- INPUTS (valeurs = artefact model_meta.json + config validee)
input double InpThreshold      = 0.3601;   // seuil |score| (q90 walk-forward, model_meta.json) ; long si score>0, short si <0
input double InpSentThreshold  = 0.15;     // veto FinBERT : pas de Buy si sent24(base)-sent24(quote) < -seuil (0=off)
input int    InpMacroMaxAgeH   = 12;       // fraicheur max de macro_features.csv (h) ; perime = AUCUN trade (features du modele)
//--- MODE DE SORTIE (v2.10) : la position SUIT le signal (institutionnel)
enum ENUM_EXIT_MODE
{
   EXIT_BARRIER=0,   // v2.00 : SL/TP fixes RR 1.70 + time-stop (structure retail, = validation)
   EXIT_SIGNAL=1     // position suit le signal : pas de TP, sortie si |score| decroit, inversion si signe change
};
input ENUM_EXIT_MODE InpExitMode = EXIT_SIGNAL; // EXIT_SIGNAL = jamais de TP (queue droite libre), SL catastrophe seul garde-fou
input double InpExitScore   = 0.15;        // sortie de position quand |score| retombe sous ce niveau (signal mort)
input double InpFullScore   = 0.60;        // |score| auquel la taille = 100% du risque (taille proportionnelle en dessous)
input double InpMinSizeFrac = 0.25;        // taille minimale (fraction du risque plein) pour un signal au seuil
input double InpCatSLATR    = 3.0;         // SL catastrophe = k x ATR14 du TF de signal — VOL TARGETING : ATR haut -> SL large -> taille reduite (taille ∝ 1/vol, servo institutionnel)
input double InpCatSLMult   = 2.0;         // repli si ATR indisponible : mult x SL pips fixes de la paire
input double InpRiskPerTrade   = 0.005;    // risque par trade (0.5% — FTMO 2-step)
input double InpRR             = 1.70;     // TP = RR x SL
input int    InpMaxHoldHours   = 168;      // time-stop (= MAX_HOLD_H entrainement)
input double InpDailyStopPct   = 0.035;    // budget de perte journalier (FTMO 5% - marge)
input double InpMaxDDPct       = 0.07;     // halt total (FTMO 10% - marge)
input int    InpMaxConcurrent  = 3;        // positions simultanees max (limite le flottant)
input int    InpMaxPerCcy      = 2;        // positions max par devise
input int    InpNoFridayAfter  = 22;       // pas d'entree vendredi apres (h srv) — regle FTMO: rien <2h avant cloture hebdo
input double InpMaxSpreadMult  = 3.0;      // spread max = mult x spread de reference
input int    InpCooldownHours  = 8;        // pause par paire apres une perte (0=off)
input int    InpDayResetOffsetH= -1;       // -1 = AUTO (minuit CE(S)T via GMT + DST europeen) ; >=0 = decalage manuel serveur->FTMO
input double InpMaxDriftSL     = 0.25;     // abandon d'un signal differe si derive prix > x*SL
input double InpMinLotRiskMult = 2.0;      // skip si le lot min risque > mult x risque cible
input double InpInitialBalance = 100000;   // solde initial FTMO : ref STATIQUE Max Loss + target ; 0 = repli peak glissant — DOIT = taille reelle du compte
input double InpTargetPct      = 0.10;     // objectif de profit : stoppe les entrees une fois atteint
input bool   InpEnsureDayTrade = false;    // false=CHALLENGE/VERIF (jours mini supprimes) ; true=COMPTE FINANCE (anti-inactivite)
input int    InpDayTradeHour   = 20;       // heure serveur du micro-lot de validation
input int    InpDayTradeEveryD = 25;       // 25=anti-inactivite (jours mini FTMO supprimes) ; 1=chaque jour vide (obsolete)
input bool   InpDryRun         = false;    // PRODUCTION : false = ordres reels envoyes ; true = signaux logges sans ordre (test)
input string InpSymbols        = "EURUSD"; // paires a trader, separees par virgules (ex: "EURUSD,GBPUSD"). Seules les 8 majeures connues du modele sont acceptees.
input ENUM_TIMEFRAMES InpSignalTF = PERIOD_H4; // timeframe des signaux. ⚠️ le modele est ENTRAINE sur H4 : un TF inferieur (H1, M30...) decale la distribution des features (atr_norm, hour, ret5...) — predictions hors domaine, a valider en dry-run/demo d'abord
input bool   InpTrendFilter    = false;    // OFF par defaut : la validation V4 a ete faite SANS filtre tendance (fidelite au test)
input int    InpTrendMAD1      = 200;      // periode SMA D1 (tendance de fond)
input int    InpTrendMAH4      = 50;       // periode SMA du TF de signal (confirmation)
input long   InpMagic          = 20260714;  // magic MIKAEL_MACRO (20260712=IA, 20260713=Donchian, 20260715=Scalp)

//--- SYMBOLES & PARAMETRES (identiques au dataset d'entrainement)
// v1.77 : liste de paires = INPUT (InpSymbols), parsee dans OnInit -> plus de
// recompilation pour changer de paires. Les tableaux d'etat sont DYNAMIQUES
// (dimensionnes a g_nsym dans OnInit). Seules les paires figurant dans SymCode()
// sont acceptees (le modele attend un sym_c connu) ; les autres sont ignorees.
string  SYMBOLS[];      // rempli par ParseSymbols() dans OnInit
int     g_nsym = 0;     // = ArraySize(SYMBOLS) apres parsing
// sym_c = index dans l'ordre TRIE : AUDJPY=0,AUDUSD=1,EURJPY=2,EURUSD=3,GBPJPY=4,GBPUSD=5,NZDUSD=6,USDJPY=7
// (SymCode reste la table complete : le modele ONNX attend sym_c=3 pour EURUSD)
// index dans SYMBOLS[] (ordre de declaration, PAS l'ordre trie de sym_c)
int SymIndex(const string s)
{
   // g_nsym peut etre momentanement > ArraySize(SYMBOLS) lors d'un re-init
   // (changement de TF/inputs : OnInit re-tourne avec les globales conservees)
   // -> borne defensive, sinon array out of range et l'EA est retire du chart
   int n=MathMin(g_nsym,ArraySize(SYMBOLS));
   for(int i=0;i<n;i++) if(SYMBOLS[i]==s) return i;
   return -1;
}
int SymCode(const string s)
{
   if(s=="AUDJPY") return 0; if(s=="AUDUSD") return 1;
   if(s=="EURJPY") return 2; if(s=="EURUSD") return 3;
   if(s=="GBPJPY") return 4; if(s=="GBPUSD") return 5;
   if(s=="NZDUSD") return 6; if(s=="USDJPY") return 7;
   return -1;
}
//+------------------------------------------------------------------+
//| Parse InpSymbols (liste CSV) -> remplit SYMBOLS[] et g_nsym.      |
//| N'accepte que les paires connues du modele (SymCode>=0), en       |
//| retirant espaces et doublons. Retourne le nombre de paires.      |
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
      if(SymCode(s)<0){ Print("[SYMBOLS] paire inconnue du modele, ignoree: '",s,"'"); continue; }
      if(SymIndex(s)>=0) continue; // doublon
      int m=ArraySize(SYMBOLS); ArrayResize(SYMBOLS,m+1); SYMBOLS[m]=s;
      g_nsym=ArraySize(SYMBOLS);  // maintenir a jour pour que SymIndex() detecte les doublons
   }
   g_nsym=ArraySize(SYMBOLS);
   return g_nsym;
}
int SlPips(const string s)
{
   if(s=="AUDJPY") return 38; if(s=="AUDUSD") return 28;
   if(s=="EURJPY") return 48; if(s=="EURUSD") return 32;
   if(s=="GBPJPY") return 59; if(s=="GBPUSD") return 42;
   if(s=="NZDUSD") return 28; if(s=="USDJPY") return 39;
   return 0;
}
// spread de reference (pips) = SPREAD_PIPS de l'entrainement
double RefSpreadPips(const string s)
{
   if(s=="AUDJPY") return 0.7; if(s=="AUDUSD") return 0.4;
   if(s=="EURJPY") return 0.6; if(s=="EURUSD") return 0.2;
   if(s=="GBPJPY") return 0.8; if(s=="GBPUSD") return 0.4;
   if(s=="NZDUSD") return 0.5; if(s=="USDJPY") return 0.3;
   return 1.0;
}

#define NFEAT 11
#define LOOKBACK 400        // bougies H4 chargees (>=SMA50+marge; voir note bars_since_flip)

long     g_onnx = INVALID_HANDLE;
CTrade   g_trade;
datetime g_lastBar[];
double   g_dayAnchor = 0.0;  // equity en debut de jour
double   g_peak      = 0.0;
int      g_dayOfYear = -1;
int      g_fileLog   = INVALID_HANDLE;
bool     g_halted    = false; // kill-switch DD max : plus AUCUNE entree (positions gerees)
string   g_gvPeak, g_gvHalt;  // persistance inter-redemarrages
string   g_gvDayA, g_gvDayD;  // ancre journaliere persistante (valeur + jour)
// signaux en attente (spread trop large a l'ouverture de bougie -> retry)
bool     g_pendActive[];
bool     g_pendLong[];
double   g_pendPred[];
datetime g_pendExpiry[];
double   g_pendRefPx[];   // prix mid au moment du signal (garde anti-derive)
// cooldown par paire apres une fermeture en perte (anti-whipsaw en range)
datetime g_coolUntil[];
string   g_gvCool; // prefixe GlobalVariable pour persistance

//+------------------------------------------------------------------+
//| Series d'indicateurs — replique EXACTE du pandas d'entrainement  |
//| (ewm adjust=False : y0=x0 ; alpha=1/n)                           |
//+------------------------------------------------------------------+
void EwmAlpha(const double &src[], const int n, double &dst[])
{
   int sz = ArraySize(src); ArrayResize(dst, sz);
   double a = 1.0/n;
   dst[0] = src[0];
   for(int i=1;i<sz;i++) dst[i] = a*src[i] + (1.0-a)*dst[i-1];
}
void SMA(const double &src[], const int n, double &dst[])
{
   int sz=ArraySize(src); ArrayResize(dst,sz);
   double sum=0;
   for(int i=0;i<sz;i++){
      sum+=src[i];
      if(i>=n) sum-=src[i-n];
      dst[i]=(i>=n-1)? sum/n : EMPTY_VALUE;
   }
}
void StdRolling(const double &src[], const int n, double &dst[]) // ddof=0
{
   int sz=ArraySize(src); ArrayResize(dst,sz);
   for(int i=0;i<sz;i++){
      if(i<n-1){ dst[i]=EMPTY_VALUE; continue; }
      double m=0; for(int k=i-n+1;k<=i;k++) m+=src[k]; m/=n;
      double v=0; for(int k=i-n+1;k<=i;k++) v+=(src[k]-m)*(src[k]-m);
      dst[i]=MathSqrt(v/n);
   }
}

//+------------------------------------------------------------------+
//| MACRO : lit macro_features.csv (v4_macro\macro_service.py).      |
//| Format 10 colonnes : ccy;sent24;sent72;sentmom;cnt24;surprise24; |
//| surprise72;fredmom;curvemom;updated_utc                          |
//| false si absent/perime/ancien format -> AUCUN trade (les features|
//| macro sont des ENTREES du modele : zero fausserait la prediction)|
//+------------------------------------------------------------------+
double g_lastSentPair=0.0;   // sent24(base)-sent24(quote) du dernier calcul
double g_lastAtr[];          // ATR14 (prix) par paire, rafraichi a chaque calcul de features — sert au SL catastrophe vol-target
bool GetMacroFeatures(const string sym, double &cal24, double &cal72,
                      double &fred2, double &fredcv, double &sentPair)
{
   int h=FileOpen("macro_features.csv",FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h==INVALID_HANDLE) return false;
   string base=StringSubstr(sym,0,3), quote=StringSubstr(sym,3,3);
   double s24b=0,s24q=0,c24b=0,c24q=0,c72b=0,c72q=0,fm=0,cv=0;
   bool gotB=false,gotQ=false; datetime updated=0;
   FileReadString(h);                                   // en-tete
   while(!FileIsEnding(h)){
      string line=FileReadString(h);
      if(line=="") continue;
      string f[]; int nf=StringSplit(line,';',f);
      if(nf<10) continue;                               // ancien format -> ignore
      if(f[0]=="USD"){ fm=StringToDouble(f[7]); cv=StringToDouble(f[8]); }
      if(f[0]==base ){ s24b=StringToDouble(f[1]); c24b=StringToDouble(f[5]); c72b=StringToDouble(f[6]); gotB=true; }
      if(f[0]==quote){ s24q=StringToDouble(f[1]); c24q=StringToDouble(f[5]); c72q=StringToDouble(f[6]); gotQ=true; }
      string ts=f[nf-1]; StringReplace(ts,"-",".");
      updated=StringToTime(ts);
   }
   FileClose(h);
   if(!gotB || !gotQ || updated==0) return false;
   if(TimeGMT()-updated>(long)InpMacroMaxAgeH*3600) return false;
   cal24=c24b-c24q; cal72=c72b-c72q;
   double side=(base=="USD")?1.0:((quote=="USD")?-1.0:0.0);
   fred2=fm*side; fredcv=cv*side;
   sentPair=s24b-s24q;
   return true;
}
// EMA pandas ewm(span, adjust=False) : y0=x0, alpha=2/(span+1)
double EmaSpanLast(const double &src[], const int span)
{
   int sz=ArraySize(src); if(sz<1) return 0.0;
   double a=2.0/(span+1.0), e=src[0];
   for(int i=1;i<sz;i++) e=a*src[i]+(1.0-a)*e;
   return e;
}
//+------------------------------------------------------------------+
//| Calcule les NFEAT=11 features V4 pour la DERNIERE bougie FERMEE  |
//| ORDRE = model_meta.json : rsi,atrn,trend200,emax,hour,dow,sym_c, |
//| cal_s24,cal_s72,fred_dgs2,fred_curve (parite 1_build_features.py)|
//+------------------------------------------------------------------+
bool ComputeFeatures(const string sym, MqlRates &r4[], MqlRates &rD[], float &out[])
{
   int n=ArraySize(r4);
   if(n<210) return false;                     // SMA200 + marge de warm-up
   double close[],high[],low[];
   ArrayResize(close,n); ArrayResize(high,n); ArrayResize(low,n);
   for(int i=0;i<n;i++){ close[i]=r4[i].close; high[i]=r4[i].high; low[i]=r4[i].low; }
   // RSI14 / ATR14 (Wilder ewm alpha=1/14, seed pandas)
   double gain[],loss[]; ArrayResize(gain,n); ArrayResize(loss,n);
   gain[0]=0; loss[0]=0;
   for(int i=1;i<n;i++){ double d=close[i]-close[i-1]; gain[i]=(d>0)?d:0; loss[i]=(d<0)?-d:0; }
   double ag[],al[]; EwmAlpha(gain,14,ag); EwmAlpha(loss,14,al);
   double tr[]; ArrayResize(tr,n); tr[0]=high[0]-low[0];
   for(int i=1;i<n;i++){
      double a=high[i]-low[i],b=MathAbs(high[i]-close[i-1]),c=MathAbs(low[i]-close[i-1]);
      tr[i]=MathMax(a,MathMax(b,c));
   }
   double atr[]; EwmAlpha(tr,14,atr);
   double sma200[]; SMA(close,200,sma200);
   int i=n-1;
   if(sma200[i]==EMPTY_VALUE || atr[i]<=0 || sma200[i]<=0) return false;
   double rsi=(al[i]>0)? 100.0-100.0/(1.0+ag[i]/al[i]) : 100.0;
   double ema8=EmaSpanLast(close,8), ema21=EmaSpanLast(close,21);
   // features macro (indispensables : pas de fichier frais = pas de trade)
   double cal24,cal72,fred2,fredcv,sent;
   if(!GetMacroFeatures(sym,cal24,cal72,fred2,fredcv,sent)){
      static datetime lastW=0;
      if(TimeCurrent()-lastW>3600){ lastW=TimeCurrent();
         Print("[MACRO] macro_features.csv absent/perime/ancien format — AUCUN trade ",
               "(verifier macro_service.py, fraicheur max ",InpMacroMaxAgeH,"h)"); }
      return false;
   }
   g_lastSentPair=sent;
   int si=SymIndex(sym);
   if(si>=0){
      if(ArraySize(g_lastAtr)<g_nsym) ArrayResize(g_lastAtr,g_nsym);
      g_lastAtr[si]=atr[i];                       // pour le SL catastrophe vol-target
   }
   MqlDateTime dt; TimeToStruct(r4[i].time,dt);   // heure d'OUVERTURE (= training)
   out[0]=(float)rsi;
   out[1]=(float)(atr[i]/close[i]);                       // atrn
   out[2]=(float)(close[i]/sma200[i]-1.0);                // trend200
   out[3]=(float)((ema8-ema21)/atr[i]);                   // emax
   out[4]=(float)dt.hour;
   out[5]=(float)(dt.day_of_week==0?6.0:(dt.day_of_week-1)); // pandas: lundi=0
   out[6]=(float)SymCode(sym);
   out[7]=(float)cal24;
   out[8]=(float)cal72;
   out[9]=(float)fred2;
   out[10]=(float)fredcv;
   for(int k=0;k<NFEAT;k++) if(!MathIsValidNumber(out[k])) return false;
   return true;
}
//+------------------------------------------------------------------+
//| (LEGACY V1, 21 features indicateurs — CODE MORT, jamais appele ; |
//| conserve pour reference du chassis d'origine)                    |
//+------------------------------------------------------------------+
bool LegacyFeatures21(const string sym, MqlRates &r4[], MqlRates &rD[], float &out[])
{
   int n=ArraySize(r4);
   if(n<60) return false;
   double close[],high[],low[],open[];
   ArrayResize(close,n);ArrayResize(high,n);ArrayResize(low,n);ArrayResize(open,n);
   for(int i=0;i<n;i++){close[i]=r4[i].close;high[i]=r4[i].high;low[i]=r4[i].low;open[i]=r4[i].open;}

   // SMA10/50
   double sma10[],sma50[]; SMA(close,10,sma10); SMA(close,50,sma50);
   // RSI14 (Wilder ewm, seed pandas)
   double gain[],loss[]; ArrayResize(gain,n); ArrayResize(loss,n);
   gain[0]=0; loss[0]=0;
   for(int i=1;i<n;i++){ double d=close[i]-close[i-1]; gain[i]=(d>0)?d:0; loss[i]=(d<0)?-d:0; }
   double ag[],al[]; EwmAlpha(gain,14,ag); EwmAlpha(loss,14,al);
   // ATR14
   double tr[]; ArrayResize(tr,n); tr[0]=high[0]-low[0];
   for(int i=1;i<n;i++){
      double a=high[i]-low[i],b=MathAbs(high[i]-close[i-1]),c=MathAbs(low[i]-close[i-1]);
      tr[i]=MathMax(a,MathMax(b,c));
   }
   double atr[]; EwmAlpha(tr,14,atr);
   // ADX14
   double pdm[],mdm[]; ArrayResize(pdm,n); ArrayResize(mdm,n); pdm[0]=0;mdm[0]=0;
   for(int i=1;i<n;i++){
      double up=high[i]-high[i-1], dn=low[i-1]-low[i];
      pdm[i]=(up>dn && up>0)?up:0; mdm[i]=(dn>up && dn>0)?dn:0;
   }
   double pdis[],mdis[]; EwmAlpha(pdm,14,pdis); EwmAlpha(mdm,14,mdis);
   double dx[]; ArrayResize(dx,n);
   for(int i=0;i<n;i++){
      double pdi=(atr[i]>0)?100.0*pdis[i]/atr[i]:0, mdi=(atr[i]>0)?100.0*mdis[i]/atr[i]:0;
      dx[i]=((pdi+mdi)>0)?100.0*MathAbs(pdi-mdi)/(pdi+mdi):0;
   }
   double adx[]; EwmAlpha(dx,14,adx);
   // Bollinger 20 / 2.5
   double bbm[],bbs[]; SMA(close,20,bbm); StdRolling(close,20,bbs);

   int i=n-1; // derniere bougie fermee
   if(sma50[i]==EMPTY_VALUE||bbm[i]==EMPTY_VALUE) return false;
   double bbup=bbm[i]+2.5*bbs[i], bblo=bbm[i]-2.5*bbs[i];
   double state=(sma10[i]>sma50[i])?1.0:((sma10[i]<sma50[i])?-1.0:0.0);
   if(state==0) return false;

   double rsi = (al[i]>0)? 100.0-100.0/(1.0+ag[i]/al[i]) : 100.0;
   double rng = high[i]-low[i]; if(rng<=0) return false;
   double body=MathAbs(close[i]-open[i])/rng;
   double upw=(high[i]-MathMax(open[i],close[i]))/rng;
   double dnw=(MathMin(open[i],close[i])-low[i])/rng;
   // swings 20
   double hi20=high[i],lo20=low[i];
   for(int k=i-19;k<=i;k++){ if(k<0)continue; hi20=MathMax(hi20,high[k]); lo20=MathMin(lo20,low[k]); }
   if(atr[i]<=0) return false;
   // bars_since_flip (dans la fenetre LOOKBACK — cf note de deploiement)
   int bsf=0;
   for(int k=i;k>0;k--){
      double stk=(sma10[k]>sma50[k])?1:((sma10[k]<sma50[k])?-1:0);
      double stp=(sma10[k-1]>sma50[k-1])?1:((sma10[k-1]<sma50[k-1])?-1:0);
      if(sma50[k-1]==EMPTY_VALUE || stk!=stp) break;
      bsf++;
   }
   // ret5
   if(i<5) return false;
   double ret5=(close[i]-close[i-5])/atr[i];

   // ---- contexte D1 ----
   int nd=ArraySize(rD); if(nd<55) return false;
   double dcl[]; ArrayResize(dcl,nd);
   // ne garder que les bougies D1 dont l'OUVERTURE est <= time de la bougie H4 (anti-lookahead)
   int nd_ok=0;
   for(int k=0;k<nd;k++){ if(rD[k].time<=r4[i].time){ dcl[nd_ok]=rD[k].close; nd_ok++; } }
   if(nd_ok<55) return false;
   ArrayResize(dcl,nd_ok);
   double dsma10[],dsma50[]; SMA(dcl,10,dsma10); SMA(dcl,50,dsma50);
   double dgain[],dloss[]; ArrayResize(dgain,nd_ok); ArrayResize(dloss,nd_ok);
   dgain[0]=0;dloss[0]=0;
   for(int k=1;k<nd_ok;k++){ double dd=dcl[k]-dcl[k-1]; dgain[k]=(dd>0)?dd:0; dloss[k]=(dd<0)?-dd:0; }
   double dag[],dal[]; EwmAlpha(dgain,14,dag); EwmAlpha(dloss,14,dal);
   int j=nd_ok-1;
   double d1state=(dsma10[j]>dsma50[j])?1.0:((dsma10[j]<dsma50[j])?-1.0:0.0);
   if(dsma50[j]==EMPTY_VALUE) d1state=0;
   double d1rsi=(dal[j]>0)?100.0-100.0/(1.0+dag[j]/dal[j]):100.0;

   MqlDateTime dt; TimeToStruct(r4[i].time,dt);

   // ---- vecteur : ORDRE EXACT de FEAT (model_meta.json) ----
   out[0]=(float)rsi;
   out[1]=(float)((close[i]-bblo)/(bbup-bblo));            // pctB
   out[2]=(float)((bbup-bblo)/bbm[i]);                     // bw
   out[3]=(float)(MathAbs(close[i]-sma50[i])/sma50[i]);    // ext
   out[4]=(float)adx[i];
   out[5]=(float)(atr[i]/close[i]);                        // atr_norm
   out[6]=(float)body;
   out[7]=(float)upw;
   out[8]=(float)dnw;
   out[9]=(float)((close[i]>open[i])?1:0);                 // bull
   out[10]=(float)((hi20-close[i])/atr[i]);                // dist_hi20
   out[11]=(float)((close[i]-lo20)/atr[i]);                // dist_lo20
   out[12]=(float)MathMin(bsf,300);                        // bars_since_flip (clip 300 = parite dataset)
   out[13]=(float)ret5;                                    // ret5_atr
   out[14]=(float)d1state;
   out[15]=(float)d1rsi;
   out[16]=(float)((d1state!=0 && d1state==state)?1:0);    // d1_aligned
   out[17]=(float)dt.hour;
   out[18]=(float)dt.day_of_week==0?6.0f:(float)(dt.day_of_week-1); // pandas: lundi=0
   out[19]=(float)((state>0)?1:0);                         // dir_b
   out[20]=(float)SymCode(sym);
   for(int k=0;k<NFEAT;k++) if(!MathIsValidNumber(out[k])) return false; // regle NaN
   return true;
}

//+------------------------------------------------------------------+
double PredictONNX(float &feats[])
{
   static matrixf X; X.Init(1,NFEAT);
   for(int k=0;k<NFEAT;k++) X[0][k]=feats[k];
   static vectorf y; y.Init(1);
   if(!OnnxRun(g_onnx, ONNX_NO_CONVERSION, X, y))
   { Print("OnnxRun echec: ",GetLastError()); return -999; }
   return (double)y[0];
}
//+------------------------------------------------------------------+
int CcyCount(const string ccy)
{
   int c=0;
   for(int i=PositionsTotal()-1;i>=0;i--){
      string s=PositionGetSymbol(i);
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      if(StringSubstr(s,0,3)==ccy || StringSubstr(s,3,3)==ccy) c++;
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
//| Sizing EXACT : perte en devise du compte pour 1 lot si SL touche |
//| (remplace l'approximation pipval 7$/10$ de la v1.00)             |
//+------------------------------------------------------------------+
double LossPerLotAtSL(const string sym, const bool longSig, const double price, const double sl)
{
   double loss=0.0;
   // OrderCalcProfit donne le P/L exact au prix sl (valeur negative attendue)
   if(OrderCalcProfit(longSig?ORDER_TYPE_BUY:ORDER_TYPE_SELL,sym,1.0,price,sl,loss) && loss<0)
      return -loss;
   // repli : tick value (cote perte si dispo)
   double tickVal=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tickVal<=0) tickVal=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE);
   double tickSz=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_SIZE);
   if(tickVal<=0||tickSz<=0) return 0.0;
   return MathAbs(price-sl)/tickSz*tickVal;
}
//+------------------------------------------------------------------+
//| Calcule le volume pour risquer riskCash. 0 = trade refuse.       |
//+------------------------------------------------------------------+
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
      // garde-fou : n'accepte le lot minimum QUE si son risque reste borne
      if(vmin*perLot>riskCash*InpMinLotRiskMult){ why="lot_min_sur_risque"; return 0.0; }
      lots=vmin;
   }
   // verif marge : il faut >=150% de la marge requise en marge libre
   double margin=0.0;
   if(OrderCalcMargin(longSig?ORDER_TYPE_BUY:ORDER_TYPE_SELL,sym,lots,price,margin)){
      if(margin*1.5>AccountInfoDouble(ACCOUNT_MARGIN_FREE)){ why="marge_insuffisante"; return 0.0; }
   }
   why="";
   return lots;
}
//+------------------------------------------------------------------+
//| Risque flottant pire-cas : perte supplementaire (devise compte)  |
//| si TOUTES les positions ouvertes (magic) touchent leur SL depuis |
//| le prix actuel. Sert au budget de perte journalier FTMO.         |
//+------------------------------------------------------------------+
double OpenRiskCash()
{
   double total=0.0;
   for(int i=PositionsTotal()-1;i>=0;i--){
      string sym=PositionGetSymbol(i);
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      double sl=PositionGetDouble(POSITION_SL);
      if(sl<=0) continue; // pas de SL = ignore (ne devrait pas arriver)
      long   ptype=PositionGetInteger(POSITION_TYPE);
      bool   isLong=(ptype==POSITION_TYPE_BUY);
      double cur=PositionGetDouble(POSITION_PRICE_CURRENT);
      // deja au-dela du SL (gap) : plus de risque restant
      if((isLong && cur<=sl) || (!isLong && cur>=sl)) continue;
      double vol=PositionGetDouble(POSITION_VOLUME);
      total+=LossPerLotAtSL(sym,isLong,cur,sl)*vol;
   }
   return total;
}
//+------------------------------------------------------------------+
//| SL/TP conformes aux stops level du broker                        |
//+------------------------------------------------------------------+
bool StopsValid(const string sym, const double price, const double sl, const double tp)
{
   double pt=SymbolInfoDouble(sym,SYMBOL_POINT);
   double minDist=SymbolInfoInteger(sym,SYMBOL_TRADE_STOPS_LEVEL)*pt;
   return (MathAbs(price-sl)>=minDist && MathAbs(price-tp)>=minDist);
}
//+------------------------------------------------------------------+
//| Time-stop : ferme les positions detenues au-dela de MaxHoldHours |
//| (l'entrainement plafonne a MAX_HOLD_H=168h — au-dela, le trade   |
//|  n'est plus dans la distribution apprise par le modele)          |
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
//| Validation des jours de trading / anti-inactivite FTMO :         |
//| logique ISOLEE dans MIKAEL_IA_DayTicket.mqh (point d'entree      |
//| unique : DayTicket_Run, appele en fin d'OnTimer)                 |
//+------------------------------------------------------------------+
#include "MIKAEL_MACRO_DayTicket.mqh"
//+------------------------------------------------------------------+
//| Heure FTMO (CE(S)T). Mode AUTO (InpDayResetOffsetH=-1) : calculee|
//| depuis TimeGMT() avec la regle DST europeenne (dernier dimanche  |
//| de mars 01:00 GMT -> dernier dimanche d'octobre 01:00 GMT).      |
//| Robuste quel que soit le fuseau du serveur broker.               |
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
      return TimeCurrent()-(long)InpDayResetOffsetH*3600;   // mode manuel
   datetime gmt=TimeGMT();
   return gmt+(IsEuDst(gmt)?2:1)*3600;                      // CE(S)T auto
}
int FtmoDayOfYear()
{
   MqlDateTime d; TimeToStruct(FtmoTime(),d);
   return d.day_of_year;
}
// minuit FTMO du jour courant, exprime en TEMPS SERVEUR (pour comparer
// POSITION_TIME / l'historique des deals)
datetime FtmoDayStartServer()
{
   return TimeCurrent()-(datetime)((long)FtmoTime()%86400);
}
//+------------------------------------------------------------------+
//| MODE SIGNAL (v2.10) : la position suit le score en continu.      |
//| A chaque bougie fermee : inversion si le score a change de camp  |
//| avec conviction (|score|>=seuil), sortie si le signal est mort   |
//| (|score|<InpExitScore). Pas de TP : la sortie EST le signal.     |
//| Tourne meme en halt (gestion de position, pas entree).           |
//+------------------------------------------------------------------+
void ManageSignalExit(const string sym, const double score)
{
   if(InpExitMode!=EXIT_SIGNAL) return;
   for(int i=PositionsTotal()-1;i>=0;i--){
      if(PositionGetSymbol(i)!=sym) continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      bool isLong=(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
      bool flip  =( isLong && score<=-InpThreshold) || (!isLong && score>= InpThreshold);
      bool decay =( isLong && score<  InpExitScore) || (!isLong && score> -InpExitScore);
      if(!flip && !decay) return;
      ulong ticket=PositionGetInteger(POSITION_TICKET);
      if(g_trade.PositionClose(ticket)){
         Print("[EXIT-SIGNAL] ",sym," ",(isLong?"long":"short")," fermee — ",
               (flip?"signal INVERSE":"signal mort")," (score=",DoubleToString(score,3),")");
         LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(isLong?"Buy":"Sell")+";"+
                DoubleToString(score,4)+";0;0;0;"+(InpDryRun?"1":"0")+
                (flip?";EXIT_FLIP":";EXIT_DECAY"));
         // si inversion : l'entree opposee est evaluee dans la MEME iteration
         // (le flux continue vers seuil/veto/TryEnter avec ce score)
      }else
         Print("[EXIT-SIGNAL] echec fermeture ",sym," ret=",g_trade.ResultRetcode());
      return;
   }
}
//+------------------------------------------------------------------+
//| Tente l'entree sur un signal valide.                             |
//| Retourne true si le signal est CONSOMME (execute ou rejete       |
//| definitivement), false s'il faut REESSAYER (spread trop large).  |
//| SL/TP ancres sur le cote de sortie (bid pour long, ask pour      |
//| short) = replique exacte du calcul de R de l'entrainement.       |
//+------------------------------------------------------------------+
bool TryEnter(const string sym, const bool longSig, const double pred, const bool canTrade,
              const double refPx=0.0)
{
   double pip=(StringFind(sym,"JPY")>=0)?0.01:0.0001;
   // mode signal : SL CATASTROPHE en k x ATR (VOL TARGETING : vol haute -> SL
   // large -> lot reduit a risque constant -> exposition ∝ 1/vol). La vraie
   // sortie est le signal (ManageSignalExit). Mode barriere : SL pips exact.
   double slp=SlPips(sym)*pip;
   if(InpExitMode==EXIT_SIGNAL){
      int si=SymIndex(sym);
      double atrp=(si>=0 && si<ArraySize(g_lastAtr))?g_lastAtr[si]:0.0;
      slp=(atrp>0)? InpCatSLATR*atrp : slp*InpCatSLMult;   // repli pips si ATR indispo
   }
   MqlTick tick; if(!SymbolInfoTick(sym,tick)) return false;   // pas de tick -> retry
   double price=longSig?tick.ask:tick.bid;

   string row=TimeToString(TimeCurrent())+";"+sym+";"+(longSig?"Buy":"Sell")+";"+
              DoubleToString(pred,4)+";1;"+
              DoubleToString(price,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS))+";";

   // --- garde anti-derive : signal differe dont le prix a trop bouge -> abandon ---
   if(refPx>0 && MathAbs(price-refPx)>InpMaxDriftSL*slp)
   { LogRow(row+"0;"+(InpDryRun?"1":"0")+";price_drift"); return true; }

   // --- filtre de spread : trop large -> on REESSAIE plus tard ---
   double sprPips=(tick.ask-tick.bid)/pip;
   if(sprPips>RefSpreadPips(sym)*InpMaxSpreadMult) return false;

   // --- contraintes portefeuille (rejets definitifs) ---
   int idx=SymIndex(sym);
   if(idx>=0 && TimeCurrent()<g_coolUntil[idx])
   { LogRow(row+"0;"+(InpDryRun?"1":"0")+";cooldown"); return true; }
   if(MagicPositions()>=InpMaxConcurrent){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";max_conc"); return true; }
   if(SymbolBusy(sym)){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";sym_busy"); return true; }
   if(CcyCount(StringSubstr(sym,0,3))>=InpMaxPerCcy ||
      CcyCount(StringSubstr(sym,3,3))>=InpMaxPerCcy)
   { LogRow(row+"0;"+(InpDryRun?"1":"0")+";ccy_corr"); return true; }

   // --- SL/TP ancres sur le prix de SORTIE (bid=long, ask=short) ---
   int digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
   double base=longSig?tick.bid:tick.ask;
   double sl=NormalizeDouble(longSig?base-slp:base+slp,digits);
   // mode signal : JAMAIS de TP (la queue droite reste libre — regle CTA)
   double tp=(InpExitMode==EXIT_SIGNAL)?0.0:
             NormalizeDouble(longSig?base+InpRR*slp:base-InpRR*slp,digits);
   if(!StopsValid(sym,price,sl,tp)){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";stops_level"); return true; }

   // --- sizing exact (tick value broker) ---
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   string why="";
   // mode signal : taille PROPORTIONNELLE a la conviction (score plein a
   // InpFullScore, plancher InpMinSizeFrac) — « quelle taille en ce moment ? »
   double sizeFrac=1.0;
   if(InpExitMode==EXIT_SIGNAL && InpFullScore>0)
      sizeFrac=MathMax(InpMinSizeFrac,MathMin(1.0,MathAbs(pred)/InpFullScore));
   double riskCash=eq*InpRiskPerTrade*sizeFrac;
   double lots=CalcLots(sym,longSig,price,sl,riskCash,why);
   if(lots<=0){ LogRow(row+"0;"+(InpDryRun?"1":"0")+";"+why); return true; }

   // --- budget de perte journalier PROSPECTIF (regle FTMO) ---
   double dayPl=(g_dayAnchor>0)?(eq-g_dayAnchor)/g_dayAnchor:0;
   double worstDay=(g_dayAnchor>0)? dayPl-(OpenRiskCash()+riskCash)/g_dayAnchor : 0;
   if(worstDay<=-InpDailyStopPct)
   { LogRow(row+"0;"+(InpDryRun?"1":"0")+";daily_budget_"+DoubleToString(worstDay*100,2)); return true; }

   if(InpDryRun){
      Print("[DRY] SIGNAL ",sym," ",(longSig?"Buy":"Sell")," pred=",DoubleToString(pred,3)," lots=",lots);
      LogRow(row+DoubleToString(lots,2)+";1;DRY_SIGNAL");
      return true;
   }
   if(!canTrade){ LogRow(row+"0;0;trade_non_autorise"); return true; }

   // type de remplissage accepte par le serveur pour CE symbole (FOK/IOC/RETURN)
   // — evite le rejet 10030 INVALID_FILL avec le FOK par defaut de CTrade
   g_trade.SetTypeFillingBySymbol(sym);
   bool ok=longSig? g_trade.Buy(lots,sym,price,sl,tp,"MIKAEL_MACRO")
                  : g_trade.Sell(lots,sym,price,sl,tp,"MIKAEL_MACRO");
   // un retry unique sur requote/prix invalide (prix rafraichi)
   uint rc=g_trade.ResultRetcode();
   if(!ok && (rc==TRADE_RETCODE_REQUOTE || rc==TRADE_RETCODE_PRICE_CHANGED || rc==TRADE_RETCODE_PRICE_OFF)){
      if(SymbolInfoTick(sym,tick)){
         price=longSig?tick.ask:tick.bid;
         base=longSig?tick.bid:tick.ask;
         sl=NormalizeDouble(longSig?base-slp:base+slp,digits);
         tp=(InpExitMode==EXIT_SIGNAL)?0.0:
            NormalizeDouble(longSig?base+InpRR*slp:base-InpRR*slp,digits);
         ok=longSig? g_trade.Buy(lots,sym,price,sl,tp,"MIKAEL_MACRO")
                   : g_trade.Sell(lots,sym,price,sl,tp,"MIKAEL_MACRO");
      }
   }
   Print("[LIVE] ",sym," ",(longSig?"Buy":"Sell")," lots=",lots," -> ",
         ok?"OK":IntegerToString(g_trade.ResultRetcode()));
   LogRow(row+DoubleToString(lots,2)+";0;"+(ok?"FILLED":"REJ_"+IntegerToString(g_trade.ResultRetcode())));
   return true;
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
int OnInit()
{
   // --- paires a trader (input CSV) -> SYMBOLS[]/g_nsym + dimensionnement des etats ---
   if(ParseSymbols(InpSymbols)<=0)
   { Print("Aucune paire valide dans InpSymbols='",InpSymbols,"' — init annulee."); return INIT_FAILED; }
   ArrayResize(g_lastBar,g_nsym);
   ArrayResize(g_pendActive,g_nsym);
   ArrayResize(g_pendLong,g_nsym);
   ArrayResize(g_pendPred,g_nsym);
   ArrayResize(g_pendExpiry,g_nsym);
   ArrayResize(g_pendRefPx,g_nsym);
   ArrayResize(g_coolUntil,g_nsym);

   g_onnx = OnnxCreateFromBuffer(ExtModel, ONNX_DEFAULT);
   if(g_onnx==INVALID_HANDLE)
   { Print("Echec chargement ONNX: ",GetLastError()); return INIT_FAILED; }
   const long inShape[]={1,NFEAT}; const long outShape[]={1,1};
   if(!OnnxSetInputShape(g_onnx,0,inShape) || !OnnxSetOutputShape(g_onnx,0,outShape))
   { Print("Echec set shapes: ",GetLastError()); return INIT_FAILED; }

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(20);
   // persistance du pic d'equity, du halt, de l'ancre du jour et des cooldowns
   // (survit aux redemarrages ; cle = magic + LOGIN pour ne jamais melanger deux comptes)
   string acc=IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string pfx="MIKAEL_MACRO_"+IntegerToString(InpMagic)+"_"+acc+"_";
   g_gvPeak=pfx+"peak";
   g_gvHalt=pfx+"halt";
   g_gvDayA=pfx+"dayanchor";
   g_gvDayD=pfx+"dayofyear";
   g_gvCool=pfx+"cool_";
   for(int i=0;i<g_nsym;i++){
      SymbolSelect(SYMBOLS[i],true); g_lastBar[i]=0; g_pendActive[i]=false;
      g_coolUntil[i]=GlobalVariableCheck(g_gvCool+SYMBOLS[i])?
                     (datetime)(long)GlobalVariableGet(g_gvCool+SYMBOLS[i]) : 0;
   }
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   g_peak=GlobalVariableCheck(g_gvPeak)? MathMax(GlobalVariableGet(g_gvPeak),eq) : eq;
   GlobalVariableSet(g_gvPeak,g_peak);
   g_halted=(GlobalVariableCheck(g_gvHalt) && GlobalVariableGet(g_gvHalt)>0.5);
   if(g_halted) Print("!! HALT persistant actif (DD max atteint precedemment) — aucune entree. ",
                      "Supprimer la variable globale ",g_gvHalt," pour re-armer.");

   // ancre journaliere (jour FTMO decale) : si on redemarre le MEME jour, on
   // reprend l'ancre sauvegardee (sinon une perte deja subie serait oubliee)
   int ftmoDay=FtmoDayOfYear();
   if(GlobalVariableCheck(g_gvDayD) && (int)GlobalVariableGet(g_gvDayD)==ftmoDay
      && GlobalVariableCheck(g_gvDayA)){
      g_dayAnchor=GlobalVariableGet(g_gvDayA);
      g_dayOfYear=ftmoDay;
      Print("Ancre journaliere restauree: ",DoubleToString(g_dayAnchor,2));
   }else{
      // ancre = BALANCE (formule FTMO : perte du jour = equity_now - balance_minuit,
      // un flottant negatif porte de la veille compte dans la perte du jour)
      g_dayAnchor=AccountInfoDouble(ACCOUNT_BALANCE);
      g_dayOfYear=ftmoDay;
      GlobalVariableSet(g_gvDayA,g_dayAnchor);
      GlobalVariableSet(g_gvDayD,g_dayOfYear);
   }

   // cooldown retroactif : pertes fermees pendant que l'EA etait eteint
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

   // journal PAR INSTANCE (suffixe magic) — coherence avec MIKAEL_DONCHIAN
   g_fileLog=FileOpen("MIKAEL_MACRO_journal_"+IntegerToString(InpMagic)+".csv",
                      FILE_READ|FILE_WRITE|FILE_SHARE_READ|FILE_TXT|FILE_ANSI);
   if(g_fileLog==INVALID_HANDLE)
      Print("!! journal CSV indisponible (err ",GetLastError(),") — l'EA continue sans log fichier");
   else if(FileSize(g_fileLog)==0)
      LogRow("time;symbol;dir;pred;signal;price;lots;dry;note");
   EventSetTimer(30);
   string symList=""; for(int i=0;i<g_nsym;i++) symList+=(i>0?",":"")+SYMBOLS[i];
   Print("MIKAEL_MACRO v2.11 init OK ⚠️ FORWARD DEMO UNIQUEMENT (modele NO-GO backtest, hypotheses sentiment+sortie en test)",
         " | exit=",(InpExitMode==EXIT_SIGNAL?
            "SIGNAL (pas de TP, taille prop. score, SL cata "+DoubleToString(InpCatSLATR,1)+"xATR [vol-target], exit<"+DoubleToString(InpExitScore,2)+")"
            :"BARRIER (RR 1.70, = validation)"),
         " | paires=",symList," (",g_nsym,") | TF=",EnumToString(InpSignalTF),
         (InpSignalTF!=PERIOD_H4?" ⚠️ HORS DOMAINE (modele entraine H4)":""),
         " | seuil |score|=",InpThreshold," | sent_veto=",
         (InpSentThreshold>0?DoubleToString(InpSentThreshold,2):"OFF"),
         " | macro_max_age=",InpMacroMaxAgeH,"h | dry_run=",InpDryRun,
         " | trend_filter=",(InpTrendFilter?"ON":"OFF (= validation)"),
         " | risk=",DoubleToString(InpRiskPerTrade*100,2),"% | daily=",
         DoubleToString(InpDailyStopPct*100,1),"% (flottant inclus) | time_stop=",InpMaxHoldHours,"h");
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_onnx!=INVALID_HANDLE) OnnxRelease(g_onnx);
   if(g_fileLog!=INVALID_HANDLE) FileClose(g_fileLog);
}
//+------------------------------------------------------------------+
//| Filtre de tendance manuel (v1.78) : SMA des clotures.            |
//| Un trade n'est autorise QUE dans le sens de la tendance          |
//| confirmee sur DEUX horizons (D1 fond + H4 confirmation).         |
//+------------------------------------------------------------------+
double SmaClose(const MqlRates &r[],int period)
{
   int n=ArraySize(r);
   if(period<=0 || n<period) return 0.0;   // pas assez d'historique -> indetermine
   double s=0.0;
   for(int i=n-period;i<n;i++) s+=r[i].close;
   return s/period;
}
// +1 = haussier confirme, -1 = baissier confirme, 0 = indetermine (aucun trade)
int TrendDir(const MqlRates &r4[],const MqlRates &rD[])
{
   double maD=SmaClose(rD,InpTrendMAD1);
   double maH=SmaClose(r4,InpTrendMAH4);
   if(maD<=0.0 || maH<=0.0) return 0;      // historique insuffisant -> on bloque (solide)
   double cD=rD[ArraySize(rD)-1].close;    // derniere bougie FERMEE (arrays non series)
   double cH=r4[ArraySize(r4)-1].close;
   if(cD>maD && cH>maH) return  1;
   if(cD<maD && cH<maH) return -1;
   return 0;                               // horizons en desaccord -> pas de tendance claire
}
//+------------------------------------------------------------------+
void OnTimer()
{
   // --- gestion des positions ouvertes (toujours, meme en halt) ---
   EnforceTimeStop();

   // --- ancre journaliere + kill switches ---
   MqlDateTime now; TimeToStruct(TimeCurrent(),now);
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   int ftmoDay=FtmoDayOfYear();
   if(ftmoDay!=g_dayOfYear){
      // ancre = BALANCE de minuit (compteur FTMO = equity_now - balance_minuit)
      g_dayOfYear=ftmoDay; g_dayAnchor=AccountInfoDouble(ACCOUNT_BALANCE);
      GlobalVariableSet(g_gvDayA,g_dayAnchor); GlobalVariableSet(g_gvDayD,g_dayOfYear);
   }
   g_peak=MathMax(g_peak,eq);
   GlobalVariableSet(g_gvPeak,g_peak);
   // les GlobalVariables MT5 expirent apres 4 semaines sans acces :
   // on rafraichit le halt tant qu'il est actif pour qu'il survive
   if(g_halted) GlobalVariableSet(g_gvHalt,1.0);
   double dayPl=(g_dayAnchor>0)?(eq-g_dayAnchor)/g_dayAnchor:0;
   // Max Loss FTMO = niveau STATIQUE (solde initial - 10%), PAS un drawdown
   // glissant : un +5% suivi d'un -7% depuis le pic laisse le compte a -2%
   // (parfaitement sain cote FTMO) et ne doit PAS halter le challenge.
   // Reference = solde initial si renseigne, sinon repli sur le pic glissant.
   double ddRef=(InpInitialBalance>0)?InpInitialBalance:g_peak;
   double dd=(ddRef>0)?(ddRef-eq)/ddRef:0;
   if(!g_halted && dd>=InpMaxDDPct){
      g_halted=true; GlobalVariableSet(g_gvHalt,1.0);
      Print("!! KILL SWITCH perte totale ",DoubleToString(dd*100,1),
            "% sous ",(InpInitialBalance>0?"le solde initial":"le pic"),
            " — entrees stoppees DEFINITIVEMENT (positions restantes gerees: SL/TP/time-stop)");
   }
   // objectif de profit atteint (balance, pas equity : les gains sont ACQUIS) :
   // plus aucune entree strategie — on ne redonne rien au marche.
   static bool targetLogged=false;
   bool targetHit=(InpInitialBalance>0 && InpTargetPct>0 &&
                   AccountInfoDouble(ACCOUNT_BALANCE)>=InpInitialBalance*(1.0+InpTargetPct));
   if(targetHit && !targetLogged){
      targetLogged=true;
      Print("== OBJECTIF DE PROFIT ATTEINT (",DoubleToString(InpTargetPct*100,1),
            "%) — entrees strategie stoppees, day-tickets maintenus ==");
   }

   bool halt = g_halted || (dayPl<=-InpDailyStopPct) || targetHit;

   // --- environnement de trading ---
   bool canTrade = !InpDryRun
                && (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
                && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED)
                && (bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED);

   for(int s=0;s<g_nsym;s++){
      string sym=SYMBOLS[s];

      // --- 1) signal en attente (spread trop large precedemment) : retry ---
      if(g_pendActive[s]){
         if(TimeCurrent()>=g_pendExpiry[s]){
            g_pendActive[s]=false;
            LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(g_pendLong[s]?"Buy":"Sell")+";"+
                   DoubleToString(g_pendPred[s],4)+";1;0;0;"+(InpDryRun?"1":"0")+";pend_timeout");
         }
         else if(!halt && !(now.day_of_week==5 && now.hour>=InpNoFridayAfter)){
            if(TryEnter(sym,g_pendLong[s],g_pendPred[s],canTrade,g_pendRefPx[s])) g_pendActive[s]=false;
         }
      }

      // --- 2) nouvelle bougie H4 ---
      datetime bt=iTime(sym,InpSignalTF,1);      // derniere bougie FERMEE du TF de signal
      if(bt==0 || bt==g_lastBar[s]) continue;
      g_lastBar[s]=bt;
      g_pendActive[s]=false;                     // un signal non execute perime a la bougie suivante
      if(now.day_of_week==5 && now.hour>=InpNoFridayAfter) continue;
      if(SymbolInfoInteger(sym,SYMBOL_TRADE_MODE)!=SYMBOL_TRADE_MODE_FULL) continue;

      // --- donnees ---
      // echec CopyRates = TRANSITOIRE (historique pas encore synchronise apres
      // une reconnexion VPS) : on rend la bougie (g_lastBar=0) pour reessayer
      // au cycle suivant au lieu de perdre le signal. La garde anti-derive
      // borne toute evaluation tardive.
      MqlRates r4[]; ArraySetAsSeries(r4,false);
      if(CopyRates(sym,InpSignalTF,1,LOOKBACK,r4)<60){ g_lastBar[s]=0; continue; } // shift=1 : bougies fermees
      MqlRates rD[]; ArraySetAsSeries(rD,false);
      if(CopyRates(sym,PERIOD_D1,1,220,rD)<55){ g_lastBar[s]=0; continue; } // 220: marge pour SMA200 du filtre tendance

      float feats[NFEAT];
      if(!ComputeFeatures(sym,r4,rD,feats)) continue;           // NaN/etat=0 -> pas de trade
      double pred=PredictONNX(feats);
      if(pred<=-900) continue;

      // --- gestion continue de la position (mode signal, meme en halt) ---
      ManageSignalExit(sym,pred);

      // V4 : score = regression sur label -1/0/+1 ; direction = SIGNE,
      // trade seulement si |score| >= seuil (q90 walk-forward)
      bool longSig=(pred>0.0);
      if(MathAbs(pred)<InpThreshold){
         MqlTick tick;
         string px=SymbolInfoTick(sym,tick)?
                   DoubleToString(longSig?tick.ask:tick.bid,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS)):"0";
         LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(longSig?"Buy":"Sell")+";"+
                DoubleToString(pred,4)+";0;"+px+";0;"+(InpDryRun?"1":"0")+";below_thr");
         continue;
      }

      // --- veto FinBERT (hypothese forward : modele + sentiment) ---
      // g_lastSentPair a ete rafraichi par ComputeFeatures (meme fichier CSV)
      if(InpSentThreshold>0){
         if(( longSig && g_lastSentPair<-InpSentThreshold) ||
            (!longSig && g_lastSentPair> InpSentThreshold)){
            LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(longSig?"Buy":"Sell")+";"+
                   DoubleToString(pred,4)+";0;0;0;"+(InpDryRun?"1":"0")+
                   ";sent_contre("+DoubleToString(g_lastSentPair,2)+")");
            Print("[SENT] ",sym," ",(longSig?"Buy":"Sell")," score=",DoubleToString(pred,3),
                  " REFUSE — sentiment news contraire (",DoubleToString(g_lastSentPair,2),")");
            continue;
         }
      }

      // --- filtre tendance manuel (v1.78) : jamais contre la tendance ---
      // le signal contre-tendance est ABANDONNE (pas mis en attente) : la
      // condition de tendance ne changera pas avant plusieurs bougies.
      if(InpTrendFilter){
         int td=TrendDir(r4,rD);
         if(td==0 || (longSig && td<0) || (!longSig && td>0)){
            LogRow(TimeToString(TimeCurrent())+";"+sym+";"+(longSig?"Buy":"Sell")+";"+
                   DoubleToString(pred,4)+";0;0;0;"+(InpDryRun?"1":"0")+";counter_trend");
            Print("[TREND] ",sym," signal ",(longSig?"Buy":"Sell")," pred=",DoubleToString(pred,3),
                  " REFUSE — tendance ",(td>0?"haussiere":(td<0?"baissiere":"indeterminee")),
                  " (D1 SMA",InpTrendMAD1,"+H4 SMA",InpTrendMAH4,")");
            continue;
         }
      }

      // prix de reference = CLOTURE de la bougie de signal (= prix d'entree suppose
      // par l'entrainement). La garde anti-derive s'applique a TOUTES les entrees :
      // couvre aussi la re-evaluation tardive d'une bougie apres un redemarrage VPS.
      double refPx=r4[ArraySize(r4)-1].close;

      // --- halt journalier : signal mis en attente (pas perdu) jusqu'a la fin ---
      // --- de la bougie en cours ; utile quand minuit FTMO tombe dans la bougie ---
      if(halt){
         g_pendActive[s]=true; g_pendLong[s]=longSig; g_pendPred[s]=pred;
         g_pendRefPx[s]=refPx;
         g_pendExpiry[s]=bt+2*PeriodSeconds(InpSignalTF); // fin de la bougie en formation
         Print("[HALT-WAIT] ",sym," signal retenu (halt journalier) — reessai apres reset du jour");
         continue;
      }

      // --- tentative d'entree ; spread trop large -> file d'attente 90 min ---
      if(!TryEnter(sym,longSig,pred,canTrade,refPx)){
         g_pendActive[s]=true; g_pendLong[s]=longSig; g_pendPred[s]=pred;
         g_pendRefPx[s]=refPx;
         g_pendExpiry[s]=TimeCurrent()+90*60;
         Print("[WAIT] ",sym," spread trop large — signal en attente (retry 30s, max 90 min)");
      }
   }

   // --- validation du jour de trading / anti-inactivite (module DayTicket) ---
   DayTicket_Run(canTrade);
}
//+------------------------------------------------------------------+
//| Cooldown : detecte les fermetures en PERTE (SL, time-stop...)    |
//| et gele la paire InpCooldownHours heures (anti-whipsaw range).   |
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
   g_pendActive[idx]=false; // un signal en attente sur cette paire est annule
   Print("[COOLDOWN] ",sym," fermee en perte (",DoubleToString(pl,2),
         ") — pas de re-entree avant ",TimeToString(g_coolUntil[idx]));
}
//+------------------------------------------------------------------+
void OnTick() { /* logique pilotee par OnTimer (30s) */ }
//+------------------------------------------------------------------+
