# Protocole v2 : tagging de tâches avant lancement

Date : 9 octobre 2026.
Issue : https://github.com/leopoldch/kaptain/issues/30
Historique : [llm-tagging-notes.md](llm-tagging-notes.md). Code : [llm_tagging/](llm_tagging/README.md).

v2 remplace la v1 du même jour. Changements : le modèle ne reçoit plus que ce
que le scheduler a réellement (pod spec, métadonnées d'image du registre) ;
`task_inputs` et `execution_context` sont supprimés ; les durées et ressources
sont mesurées dans Docker.

## 1. Question

À partir de ce qu'un scheduler sait avant de lancer un pod, un modèle peut-il
dire quel type de travail il fait et combien de temps il dure ? Avec quelle
qualité, quelle latence et quel coût ?

Un tag `cpu` décrit du travail de calcul demandé. Il ne prouve pas que le CPU
sera le goulot d'étranglement. Le bottleneck réel et le retard sont hors pilote.

## 2. Entrées

Un appel par tâche, contexte neuf, température 0. Deux niveaux :

| Niveau | Contenu | Disponible au scheduling ? |
|---|---|---|
| `pod` | Le pod spec tel que dans le manifest | Oui, c'est l'objet que reçoit le scheduler |
| `pod_image` | Pod spec + config de chaque image lue sur le registre | Oui, au prix d'une requête au registre (mesurée) |

Retirés du pod spec : `metadata` (nom, labels comme `kaptain.io/workload`),
`schedulerName`, commentaires YAML. Tout le reste est envoyé tel quel, avec les
quantités Kubernetes d'origine (`500m`, `64Mi`).

Métadonnées d'image, lues sans télécharger les couches : architecture, OS,
taille compressée totale, nombre de couches, entrypoint, cmd, env, ports
exposés, répertoire, utilisateur, 20 dernières lignes d'historique (200
caractères max chacune). Pas de taille décompressée : le registre ne la donne pas.

Pas de description des données d'entrée : Kubernetes n'en a pas. Pas de
caractéristiques du nœud : le nœud est justement ce que le scheduler choisit.

## 3. Tags de type de travail

| Tag | Définition |
|---|---|
| `cpu` | Calcul : compression, transformation, encodage, calcul numérique, stress CPU |
| `memory` | Allocation/occupation mémoire significative ou travail sur de grands buffers |
| `disk` | Lecture/écriture de fichiers au cœur de l'opération |
| `network` | Transferts réseau ou traitement de trafic au cœur de l'opération |
| `wait` | Essentiellement attendre/dormir ; toujours seul |
| `unknown` | Informations insuffisantes ; toujours seul |

Plusieurs tags parmi cpu/memory/disk/network sont permis. Les requests/limits
ne prouvent pas le travail. Un serveur web dont le trafic n'est pas décrit est `unknown`.

## 4. Tags de durée

Temps du processus applicatif, de son démarrage à sa fin, sans pull ni scheduling.

| Tag | Durée |
|---|---|
| `short` | < 10 s |
| `medium` | 10 s inclus à < 60 s |
| `long` | >= 60 s |
| `unknown` | Pas déductible des entrées |

`activeDeadlineSeconds` est une limite, pas une estimation. Un nombre
d'opérations sans calibration matérielle ne donne pas de durée.

## 5. Sortie

JSON imposé par schéma : `work_types`, `duration`, `evidence` (≤ 3),
`missing_information` (≤ 3). Seuls les deux premiers sont notés. Une réponse
qui viole les règles (ex. `wait` combiné) est invalide et compte comme fausse.

## 6. Vérité terrain

Deux références séparées, jamais envoyées au modèle :

- `references.json` : ce qui est **déductible** du pod spec (ex. `stress-ng
  --cpu-ops 6000` → durée `unknown`). Brouillon écrit par Claude, à relire par Léo.
- `measurements.json` : ce que la tâche **fait réellement**. Chaque cas tourne
  3 fois dans Docker avec les limites CPU/mémoire et l'utilisateur du pod.
  Durée = horodatages Docker de début/fin du processus. CPU, pic mémoire et
  octets disque lus dans le cgroup toutes les 0,2 s. Classe mesurée = majorité
  des 3 ; un cas dont les runs changent de classe est marqué instable.

Une machine (Ryzen 7 2700X), Docker et non Kubernetes : les durées mesurées
valent pour ce matériel. C'est justement pourquoi un nombre d'opérations ne
donne pas de durée sans connaître le nœud.

## 7. Mesures rapportées

Par modèle et niveau :

- réponses valides ;
- type de travail : ensemble exact, précision/rappel par tag ;
- durée vs référence déductible ;
- durée vs classe mesurée, sur les réponses non-`unknown` seulement, avec le
  taux de réponse ;
- abstentions (`unknown`) ;
- latence d'appel : médiane, p90, max, sur des appels avec modèle déjà chargé
  (un appel de chauffe non compté le précède) ; chargement à froid à part ;
- tokens entrée/sortie, coût API ; temps de lecture du registre pour `pod_image`.

Répétitions : 3 appels identiques. À température 0 elles mesurent la latence,
pas la stabilité ni des tâches indépendantes. Les processus GPU présents au
début et à la fin sont enregistrés : un autre programme sur le GPU fausse la latence.

Pas de baseline à règles écrites à la main : elles reviendraient à coder en dur
un scheduler pour des programmes connus, et répondraient `unknown` dès qu'un
programme n'est pas prévu. Une première version, écrite en connaissant les 12
cas, obtenait 100 % pour cette seule raison ; elle a été retirée.

## 8. Limites

12 cas, dont la commande révèle souvent la réponse : ce pilote mesure surtout
si un modèle suit des définitions et à quel coût, pas sa valeur sur des tâches
réelles opaques. L'étape suivante est un jeu de pods dont la commande ne dit
pas tout (scripts, images applicatives), avec mesures.
