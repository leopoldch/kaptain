# Notes de discussion : tagging de tâches par LLM

Date : 9 octobre 2026.
Issue : https://github.com/leopoldch/kaptain/issues/30

Ce fichier conserve les pistes discutées. Il ne décrit pas un protocole déjà
arrêté ni des résultats obtenus. Pour le moment, nous discutons avant de coder
ou de lancer des expériences.

Le 9 octobre, l'utilisateur demande de formaliser les entrées exactes,
les tags de travail multiples et les quatre classes de durée, puis d'annoter
manuellement les tâches avant comparaison. La spécification initiale est
dans [llm-tagging-protocol.md](llm-tagging-protocol.md). Elle distingue types
de travail, bottlenecks réels et durée déductible versus durée mesurée.
Les références initiales et seuils de durée y sont explicités ; aucun appel
de modèle n'a encore été lancé.

## Question de recherche

Un LLM peut-il extraire des informations utiles sur une tâche à partir de ce
qui est disponible avant son lancement ? Quelles entrées supplémentaires
améliorent cette compréhension, et pour quel coût ?

Entrées envisagées :

- Manifest : requests, limits, commande, arguments, variables et labels.
- Image Docker : nom, taille, couches et métadonnées accessibles.
- Inputs : description, format, taille et quantité de données disponibles.

Comparer les mêmes tâches avec manifest seul, puis manifest + image, puis
manifest + image + inputs. Il faut comptabiliser le coût de collecte et de
préparation des entrées, pas uniquement celui de l'appel LLM.

## Première étape : reconnaître les tâches

Commencer par les workloads cpu, memory, disk, web et noop de la PR #28 :
https://github.com/leopoldch/kaptain/pull/28
Ils ne sont pas encore présents sur mainline au moment de notre exploration.

Séparer la ressource dominante (CPU, mémoire, disque, réseau, mixte) de la
durée attendue (courte, longue, indéterminée). Une commande CPU ne suffit pas
à connaître sa durée : volume de travail, matériel et concurrence comptent.
Une tâche web peut avoir plusieurs contraintes selon les requêtes reçues.

Les tags initiaux seraient des références fondées sur la construction des
tâches. Des mesures d'exécution pourraient ensuite confirmer leur comportement
sur un matériel donné. Ce ne sont pas encore des labels empiriques validés.

Masquer les noms et labels qui révèlent artificiellement la classe attendue.
Conserver les indices réels comme les commandes, mais comparer à des règles
simples : reconnaître `stress-ng --cpu` ne démontre pas à lui seul la valeur
d'un LLM. Tester ensuite des tâches moins explicites pour la généralisation.

## Coût et chemin de scheduling

Le tagging doit rester assez peu coûteux pour être utile. Mesurer précision,
abstentions, latence de bout en bout, tokens et consommation de ressources.
Comparer aussi démarrage à froid et modèle déjà chargé, ainsi que les latences
médiane et de queue sous plusieurs requêtes.

Piste : préparer les tags en amont et les mettre en cache. La clé doit couvrir
les informations qui influencent le tag, notamment digest de l'image,
commande/configuration et caractéristiques pertinentes des inputs.

Un appel en arrière-plan n'aide pas le placement courant si son résultat arrive
après la décision. Il peut aider des exécutions suivantes. Une politique de
repli sur tag absent reste à définir. Faire tourner l'inférence sur un worker
mesuré peut aussi perturber la charge : cela doit être compté dans le bilan.

## Modèles locaux

Ollama est une piste pour comparer des modèles locaux, avec réponses JSON
courtes et structurées. La rapidité dépend du matériel, du modèle, de la
quantification, du prompt et de la concurrence ; aucune latence n'a été mesurée.

L'API fournit notamment les durées de chargement, traitement du prompt et
génération, ainsi que les nombres de tokens. Mesurer aussi le temps côté client.
Source : https://github.com/ollama/ollama/blob/main/docs/api.md

Le matériel de test et les modèles restent à choisir. Aucun modèle n'a encore
été installé ou lancé pour cette expérience.

## Deuxième étape possible : prédire un retard

Piste discutée : un classifieur léger produit une probabilité de retard à partir
des caractéristiques de tâche, éventuellement enrichies par le LLM, et de
l'état du cluster ou du nœud envisagé.

Il faut d'abord choisir la cible : attente avant démarrage, ralentissement
d'exécution par rapport à une référence, ou dépassement de deadline. Ce sont
des problèmes distincts. Le seuil et la référence de ralentissement restent
à définir.

Comparer le même classifieur avec et sans les informations LLM, sur les mêmes
données et splits. Le gain doit compenser le coût d'extraction de ces informations.
Évaluer sur des exécutions futures et des profils de tâches non vus ; éviter
de placer des copies quasi identiques de part et d'autre du split.

L'idée d'agir au-dessus de 85 % est une hypothèse, pas une règle retenue. Une
confiance écrite par le LLM n'est pas une probabilité calibrée. Même les
probabilités d'un classifieur doivent être vérifiées sur des données séparées.
Le seuil dépendra des faux positifs, faux négatifs et du coût de l'action.
Source : https://scikit-learn.org/stable/modules/calibration.html

Une bonne prédiction de retard ne prouve pas qu'un autre placement aurait été
meilleur. Cette utilité nécessitera une comparaison de politiques en exécution.

## A-t-on besoin d'un vrai cluster ?

Pour le premier pilote de tagging : non. Un ordinateur et un modèle local
suffisent pour fournir les entrées statiques, comparer les tags aux références
et mesurer le coût d'inférence. Aucun pod ne doit être déployé pour ce test.
Les latences obtenues ne valent que pour la machine qui exécute le modèle.

Pour vérifier les ressources réellement sollicitées : les tâches peuvent
d'abord être exécutées dans des conteneurs sur une machine, avec collecte CPU,
mémoire, I/O et temps. Un cluster multinœud n'est pas indispensable à cette
étape, mais les observations dépendent de ce matériel et des charges choisies.

Pour prototyper la prédiction de retard : des données d'exécution étiquetées
sont nécessaires. On peut commencer par des expériences locales sous charges
contrôlées ou des simulations. Cela valide le pipeline et une hypothèse dans
ce contexte ; cela ne prouve pas les performances sur le cluster cible.

Pour démontrer un meilleur placement dans Kaptain : oui, des exécutions sur un
cluster avec plusieurs nœuds éligibles sont nécessaires. Pour des conclusions
sur la performance matérielle ou l'hétérogénéité, utiliser des machines/VM
avec ressources distinctes et documentées. kind sur un seul hôte sert surtout
à valider le câblage ; un seul candidat éligible ne teste pas le choix de nœud.

Progression envisagée : tagging offline, vérification locale des tâches,
prédiction à partir de données mesurées, puis validation du placement sur le
laboratoire K3s existant. Pas besoin d'attendre le cluster pour commencer le
tagging.

## Points encore ouverts

- Modèles, matériel d'inférence et budget de latence acceptable.
- Taxonomie et définition des références, notamment pour les tâches mixtes.
- Description des inputs à envoyer et quantité d'information utile.
- Définition précise du retard et action déclenchée par sa prédiction.
- Cache, moment d'extraction et comportement sur tag manquant.
- Taille du jeu de tâches et protocole de généralisation.
- La faible exploration de ces entrées dans les papiers est une intuition
  évoquée dans la discussion ; vérifier la littérature avant de revendiquer
  une nouveauté scientifique.

## Livrable visé

Un tableau par modèle et niveau d'entrées : qualité des tags, abstentions,
latences, tokens et ressources consommées. Puis, si nous poursuivons la piste
de classification, le gain prédictif apporté par les informations LLM et son
coût. Aucun résultat n'est disponible à ce stade.

## Proposition de pilote local, discutée le 9 octobre

Lecture de la PR #28 : CPU, mémoire et disque partagent exactement l'image
`ghcr.io/colinianking/stress-ng`, mais utilisent respectivement `--cpu`, `--vm`
et `--hdd`. Web utilise nginx-unprivileged et noop busybox. Les images sont
fixées par digest. Ce sont des adaptations des scénarios DRS, pas leurs images
originales : le README explicite notamment le remplacement du réseau par la
mémoire. Vérifier séparément les images originales avant de les reproduire.

Ces commandes peuvent être exécutées avec Docker en local, en traduisant les
limites et la configuration pertinentes du manifest. Cela ne reproduit pas
les mécanismes de scheduling Kubernetes. Télécharger les images une fois,
séparer le temps de téléchargement du temps d'exécution, mesurer CPU, mémoire,
I/O et durée, puis comparer aux tags prédits avant exécution.

Deux pièges : noop est `sleep 60`, donc faible travail mais pas durée courte ;
nginx sans client reste surtout en attente, donc son image ne prouve pas une
charge réseau. Une variante avec trafic local explicite serait nécessaire.
Un stressor disque n'est pas garanti I/O-bound sur tout stockage : vérifier
les mesures et distinguer écritures en cache et activité du périphérique.

Le PC inspecté a un Ryzen 7 2700X, environ 16 Go de RAM et une RTX 2070 8 Go.
Docker répond ; Ollama est installé et sa liste de modèles est vide.
Proposition de modèles (non arrêtée) : Qwen3 1.7B et 4B avec réflexion désactivée
pour la vitesse, puis GPT-5.4 mini via API comme comparateur. GPT-5.4 nano a
aussi été envisagé, mais sa page officielle est marquée deprecated lors de
la vérification : ne pas en faire le choix par défaut. La taille des poids
seule ne garantit pas la place en VRAM :
contexte, cache et autres usages GPU comptent.
Sources : https://ollama.com/library/qwen3 ;
https://developers.openai.com/api/docs/models/gpt-5.4-nano ;
https://developers.openai.com/api/docs/models/gpt-5.4-mini

Pilote proposé : 5 tâches x 3 niveaux d'entrées x 3 répétitions par modèle.
Ces répétitions mesurent notamment la latence et la stabilité ; ce ne sont pas
45 tâches indépendantes et elles ne suffisent pas à démontrer la généralisation.
Pour tester réellement l'apport des inputs, ajouter des variantes où leur
volume ou format change le travail. Les stressors actuels ne fournissent pas
tous un troisième niveau d'information distinct.

L'utilisateur envisage ses crédits OpenAI pour un premier essai. Aucun appel
payant, téléchargement de modèle ni lancement de workload effectué. Définir
un budget et borner requêtes/tokens avant un essai. Envoyer une description
textuelle extraite de l'image, pas ses couches binaires. Conserver les labels
de référence et les mesures post-exécution hors du prompt.

Exemple de budget API : 45 appels à GPT-5.4 mini, chacun avec 2 000 tokens
d'entrée et 150 tokens de sortie facturés, coûtent environ 0,098 USD au tarif
standard de 0,75 USD / million en entrée et 4,50 USD / million en sortie.
C'est une estimation conditionnelle : davantage de tokens, du raisonnement
ou des retries augmentent le total. Proposition de budget pilote : 1 USD,
avec arrêt contrôlé par le runner ; ce budget n'est pas encore approuvé.

## Mesurer le tagging et envisager un seuil

Mesurer chaque appel avec une horloge monotone côté client : début juste
avant l'envoi, fin après réception de la réponse complète. Enregistrer
séparément la préparation des entrées et la validation du JSON. La latence
utile de bout en bout comprend aussi ces étapes. Le premier token ne suffit
pas pour agir si le tag structuré complet n'est pas encore disponible.

Conserver les métriques internes Ollama, les tokens API, les erreurs/timeouts,
le modèle exact et sa configuration. Rapporter les appels à froid et à chaud
séparément. Les répétitions donnent une distribution de latences, pas une
durée universelle. Un petit pilote ne permet pas une estimation robuste du p95.

Évaluer les tags contre des références indépendantes : intention connue du
workload d'abord, puis comportement mesuré dans des conditions documentées.
Ne pas attribuer automatiquement réseau à nginx, I/O-bound à tout stressor
disque, ou courte à noop. Définir chaque tag avant l'évaluation ; autoriser
mixte et inconnu, et rapporter exactitude, précision/rappel par tag et couverture.
Un tag mémoire peut désigner capacité occupée ou bande passante : préciser
la définition retenue plutôt que confondre ces comportements.

L'utilisateur propose une classification « critique », un seuil et mentionne
« jev ». Le sens de « jev » n'est pas encore connu ; ne pas supposer un modèle
ou une méthode. Clarifier aussi critique : risque de deadline dépassée,
ralentissement, attente ou priorité déclarée par l'utilisateur.

On peut ajouter un classifieur de risque séparé, mais il lui faut des exemples
étiquetés, une cible définie et un test indépendant. Un seuil de 0,85 sur une
probabilité calibrée peut déclencher une action ou une alerte ; il ne garantit
pas 85 % de précision parmi tous les cas au-dessus de ce seuil. Mesurer cette
précision et le rappel réellement obtenus, ainsi que les coûts d'erreurs.
Sous le seuil, le modèle n'affirme pas forcément que la tâche est sans risque.

Pour filtrer les tags eux-mêmes, on peut aussi accepter seulement les
prédictions dont la fiabilité a été validée, et s'abstenir sinon. Le seuil
confiance du tagging et le seuil probabilité de retard répondent à deux
questions distinctes. Les cinq familles du pilote ne suffisent pas à
entraîner/calibrer et valider sérieusement un modèle de risque.

## Passage à la v2, 9 octobre (soir)

Premiers runs (qwen3 1.7B, 4B, 8B, cas dev) : aucun modèle n'utilise `unknown`,
tous prennent les requests pour du travail, `wait` est combiné à d'autres tags.
Ces runs utilisaient des fixtures inexactes (limites mémoire de c02/c03 à 64Mi
au lieu de 256Mi dans la PR #28) et des inputs écrits à la main.

Décision de Léo : ne garder que l'information disponible au scheduling.
`task_inputs` est supprimé. Le modèle reçoit le pod spec réel, puis les
métadonnées d'image lues sur le registre (sans pull). Durées et ressources
sont mesurées dans Docker. Voir [llm-tagging-protocol.md](llm-tagging-protocol.md) v2.
