# Design du plugin Go in-scheduler

Ce document décrit le plugin Go de Kaptain. Son rôle est de fixer précisément ce que le plugin fait, quelles métriques il rend plus faciles à mesurer, et comment il reste comparable avec le chemin HTTP extender.

**État au 17 septembre 2026 : implémenté** dans `plugin/`, compilé contre Kubernetes v1.31.0. Points d'extension réalisés : `PreScore`, `Score`, `NormalizeScore`, `Reserve`/`Unreserve` et `PostBind` (qui journalise le nœud réellement lié, seul enregistrement fiable du placement quand les scores sont à égalité). Ces cinq points doivent être activés dans le profil : un `PostBind` absent du profil supprime silencieusement la journalisation du placement. Stratégies locales : `dummy-random` (tirage à graine), `largest-cpu-capacity`, `least-allocated`. Décideur externe optionnel avec délai d'attente. Métriques, logs JSON et fallback explicite conformes aux sections ci-dessous. Non réalisé à ce stade : `PreFilter`, cache de télémétrie en arrière-plan, et toute stratégie ML/RL/LLM, qui passent par le décideur externe.

## Objectif

Le plugin sert à comparer deux intégrations d'une même politique de placement :

```text
Extender : kube-scheduler → HTTP /prioritize → stratégie Python/ML/LLM → scores
Plugin   : kube-scheduler → plugin Go Score → stratégie locale ou décideur externe → scores
```

Le but n'est pas de changer la question scientifique. Le plugin doit garder le même problème que l'extender : choisir un nœud faisable pour le prochain Pod, sans migration, préemption, changement de réplicas ni contrôle d'alimentation. Ce qui change est uniquement le point d'intégration dans Kubernetes et donc le coût de décision, l'accès aux métriques du scheduler et la robustesse opérationnelle.

## Pourquoi garder un plugin en plus de l'extender

L'extender est utile pour avancer vite : Python, modèles ML, LLM local, logs lisibles, itération simple. Le plugin Go permet de tester une intégration plus proche du Scheduling Framework, plus proche de DRS dans l'esprit, et moins dépendante d'un aller-retour HTTP externe.

La comparaison doit répondre à une question précise : à politique équivalente, est-ce que l'intégration in-scheduler réduit la latence, les erreurs, les fallbacks ou l'overhead de contrôle ? Si les résultats sont proches, l'extender peut rester le chemin principal. Si l'extender devient le goulot, le plugin devient le chemin de production expérimentale.

## Périmètre du premier plugin

Le premier plugin doit rester volontairement minimal.

Il doit faire :

- recevoir le Pod et les nœuds candidats déjà filtrés par Kubernetes ;
- construire un snapshot de décision commun avec l'extender ;
- scorer chaque candidat ;
- normaliser les scores dans l'échelle attendue par Kubernetes ;
- appliquer un fallback explicite si la stratégie échoue ou dépasse son budget ;
- journaliser chaque décision avec un `run_id`, `pod_uid`, `strategy`, `integration=plugin`, `policy_version`, candidat choisi, score et raison de fallback si applicable ;
- exposer des métriques Prometheus propres au plugin.

Il ne doit pas faire au début :

- éviction ou déplacement de Pods déjà placés ;
- preemption personnalisée ;
- autoscaling ;
- génération de code par LLM ;
- requêtes Prometheus bloquantes à chaque candidat ;
- entraînement en ligne directement dans le processus kube-scheduler ;
- fine-tuning LLM ;
- logique globale de packing comme Christensen.

## Extension points Kubernetes visés

Le plugin minimal doit viser principalement `Score` et `NormalizeScore`.

`Score` calcule un score brut pour chaque nœud candidat. C'est l'équivalent naturel de l'endpoint `/prioritize` de l'extender. Il permet de tester les mêmes heuristiques, puis les mêmes politiques ML/RL/LLM adaptées.

`NormalizeScore` transforme les scores bruts dans l'échelle Kubernetes attendue. Cela évite que les stratégies aient à connaître les détails de normalisation du scheduler. La normalisation doit être documentée et identique entre méthodes.

`PreScore` est souhaitable dès que possible, mais pas obligatoire pour le tout premier prototype. Il permet de construire une seule fois le snapshot commun pour un Pod avant de scorer tous les nœuds. Sans `PreScore`, il faut éviter de reconstruire ou relire l'état complet dans chaque appel `Score`.

`Reserve` et `Unreserve` sont utiles plus tard pour suivre les ressources réservées par des Pods qui ont été choisis mais ne sont pas encore visibles comme running. Cela aide les heuristiques request-aware et les modèles à éviter de surcharger un nœud entre deux décisions rapprochées. Pour le premier plugin, on peut commencer sans `Reserve/Unreserve` si le runner est lent et si l'on documente cette limite. Pour des expériences sérieuses, il faudra les ajouter ou tenir un suivi équivalent par watch API.

Règle à retenir : **pour une politique de placement classique, le bon premier plugin est `Score`.** Un `Filter` sélectif peut forcer une décision unique, mais il faut alors le documenter comme tel : le plugin transforme la décision en contrainte, pas en score. C'est exactement ce que fait DRS, dont le plugin `dqn-plugin` est un `Filter` qui renvoie `UnschedulableAndUnresolvable` pour tous les nœuds sauf celui choisi par son décideur, ce qui rend la phase de scoring sans effet. Conséquence à ne pas reproduire : quand l'appel au décideur échoue, DRS laisse passer tous les nœuds et le scoring par défaut décide en silence. Notre fallback doit être explicite, journalisé et compté, comme dans `bridge/decision.py`.

`Filter` n'est pas prioritaire. Kubernetes filtre déjà les nœuds infaisables. Le plugin doit d'abord scorer les candidats fournis par le scheduler. Ajouter un `Filter` custom risquerait de changer le problème expérimental en introduisant des contraintes propres au plugin.

`Bind` n'est pas nécessaire au début. Laisser Kubernetes binder le Pod garde l'expérience simple et comparable à l'extender.

## Données d'entrée du snapshot

Le plugin doit produire le même schéma logique que l'extender. Ce snapshot est l'objet central de comparaison.

Pour le Pod :

- UID, namespace, nom ;
- containers et requests/limits CPU/mémoire ;
- image, command, args, labels et annotations utiles ;
- `schedulerName` ;
- timestamps disponibles ;
- famille de workload si le runner l'encode ;
- contraintes déclarées, par exemple nodeSelector, affinity, taints/tolerations déjà résolues par Kubernetes autant que possible.

Pour chaque nœud candidat :

- nom du nœud ;
- capacité et allocatable CPU/mémoire ;
- ressources demandées déjà réservées ou observées ;
- nombre de Pods ;
- conditions du nœud ;
- métriques CPU/RAM récentes si disponibles ;
- métriques réseau/disque si validées ;
- âge de chaque mesure ;
- indicateur de donnée manquante.

Pour le contexte global :

- `run_id` ;
- version de la stratégie ;
- version du modèle ou de la politique ;
- seed/tie-breaker ;
- horodatage monotone local ;
- horodatage wall-clock pour corrélation externe ;
- version du cache de télémétrie ;
- taille du candidat set ;
- budget de décision.

Une donnée absente doit rester absente. Elle ne doit pas être transformée silencieusement en zéro.

## Stratégies prévues dans le plugin

Le plugin doit d'abord implémenter ou appeler les stratégies simples, parce qu'elles servent à valider l'intégration.

Ordre conseillé :

1. `dummy-random` avec seed contrôlée, pour vérifier que le plugin influence réellement le placement.
2. `largest-cpu-capacity`, pour obtenir une équivalence avec l'extender actuel.
3. `least-allocated` et `most-allocated` request-aware, avec suivi des Pods déjà réservés.
4. heuristique télémétrie simple, utilisant le même cache que l'extender.
5. appel vers un décideur externe ML/RL/LLM si nécessaire.
6. DRS-style DQN adapté, une fois l'état et les métriques stabilisés.

Pour ML/RL/LLM, le plugin a deux options.

Option 1 : stratégie locale Go. Elle est adaptée aux heuristiques, aux modèles très simples ou à un modèle exporté dans un format facile à charger. C'est le chemin le plus faible en overhead.

Option 2 : décideur externe. Le plugin reste dans le scheduler, mais appelle un service local pour la partie ML/RL/LLM. Ce chemin ressemble à DRS : scheduler intégré, décision externe. Il faut alors mesurer l'appel externe comme une sous-partie de la décision plugin, pas le confondre avec le coût du Scheduling Framework lui-même.

## Métriques spécifiques au plugin

Le plugin doit faciliter les métriques suivantes. Les noms exacts pourront changer, mais les dimensions doivent rester stables.

| Métrique | Type | Labels contrôlés | Sens |
|---|---|---|---|
| `kaptain_plugin_score_duration_seconds` | histogram | `strategy`, `integration`, `status` | Durée totale de scoring côté plugin pour une tentative. |
| `kaptain_plugin_snapshot_duration_seconds` | histogram | `strategy`, `status` | Temps de construction du snapshot. |
| `kaptain_plugin_decider_duration_seconds` | histogram | `strategy`, `decider`, `status` | Temps passé dans la stratégie locale ou l'appel au décideur externe. |
| `kaptain_plugin_normalize_duration_seconds` | histogram | `strategy` | Temps de normalisation des scores. |
| `kaptain_plugin_candidate_nodes` | histogram | `strategy` | Nombre de nœuds candidats vus par décision. |
| `kaptain_plugin_fallback_total` | counter | `strategy`, `reason` | Nombre de décisions où un fallback a été utilisé. |
| `kaptain_plugin_invalid_decision_total` | counter | `strategy`, `reason` | Choix invalide, score invalide, candidat absent, timeout, format invalide. |
| `kaptain_plugin_cache_age_seconds` | histogram | `source` | Âge des métriques utilisées dans le snapshot. |
| `kaptain_plugin_missing_features_total` | counter | `feature_group` | Données absentes dans les snapshots. |
| `kaptain_plugin_decisions_total` | counter | `strategy`, `status` | Nombre de décisions tentées et leur résultat. |

Les labels ne doivent pas contenir `pod_uid`, `node_name`, image ou workload individuel dans Prometheus, pour éviter une cardinalité explosive. Ces informations vont dans les logs structurés, pas dans les labels métriques.

## Logs structurés par décision

Chaque décision doit produire une ligne JSON, ou un événement équivalent, avec les champs suivants :

```json
{
  "run_id": "...",
  "integration": "plugin",
  "strategy": "least-allocated",
  "policy_version": "...",
  "pod_uid": "...",
  "pod_namespace": "...",
  "pod_name": "...",
  "candidate_count": 3,
  "chosen_node": "worker-1",
  "scores": [
    {"node": "worker-1", "raw": 0.21, "normalized": 100},
    {"node": "worker-2", "raw": 0.57, "normalized": 42}
  ],
  "fallback": false,
  "fallback_reason": null,
  "snapshot_age_ms": 840,
  "durations_ms": {
    "snapshot": 0.8,
    "decider": 1.4,
    "normalize": 0.1,
    "total": 2.5
  }
}
```

Les scores détaillés peuvent être trop volumineux pour les grands clusters. Pour les grands tests, conserver au minimum le candidat choisi, le meilleur score, le deuxième score, le nombre de candidats et un hash du snapshot. Le mode debug peut conserver tous les scores.

## Mesures comparables extender vs plugin

La comparaison doit isoler l'effet de l'intégration.

| Dimension | Extender | Plugin |
|---|---|---|
| Point d'entrée | HTTP `/prioritize` | Extension point `Score` |
| Transport externe | Oui, scheduler → service extender | Non si stratégie locale ; oui si décideur externe |
| Sérialisation | JSON Kubernetes extender | Objets Go internes ; sérialisation seulement si décideur externe |
| Mesure propre | handler + middleware + métriques scheduler | timers internes plugin + métriques scheduler |
| Risque principal | RPC, parsing, timeout, service indisponible | complexité de build scheduler, couplage version Kubernetes |
| Avantage principal | itération rapide Python/LLM | mesure plus directe dans le scheduler, moins de transport |

Pour comparer proprement, il faut exécuter la même stratégie sous les deux intégrations quand c'est possible. Une comparaison `LLM via extender` contre `heuristique via plugin` ne mesure pas l'intégration ; elle mélange politique et intégration.

Les premiers tests utiles sont donc :

1. random extender vs random plugin ;
2. largest-cpu-capacity extender vs largest-cpu-capacity plugin ;
3. least-allocated extender vs least-allocated plugin ;
4. heuristique télémétrie extender vs heuristique télémétrie plugin ;
5. ML/RL/LLM seulement après validation des quatre précédents.

## Fallbacks

Le fallback doit être identique conceptuellement entre extender et plugin.

Cas de fallback :

- snapshot incomplet au-delà du seuil autorisé ;
- cache trop vieux ;
- stratégie qui panique ou renvoie une erreur ;
- score NaN/infini ;
- timeout du décideur externe ;
- aucun score valide ;
- candidat choisi absent de la liste fournie par Kubernetes.

Fallback initial recommandé : least-allocated request-aware avec tie-breaker déterministe. Il est simple, explicable et ne dépend pas de télémétrie live. Le fallback doit être compté comme une décision de la méthode, pas retiré des résultats.

## Intégration avec l'observabilité

Le plugin ne doit pas interroger Prometheus dans `Score` pour chaque Pod. Il doit lire un cache mis à jour en arrière-plan ou recevoir un snapshot déjà maintenu par un composant commun. Cette contrainte est la même que pour l'extender.

Les métriques kube-scheduler natives doivent être utilisées comme contrôle :

- durée end-to-end de scheduling ;
- durée des extension points ;
- nombre de Pods pending ;
- tentatives et erreurs de scheduling.

Les métriques Kaptain ajoutent le détail que Kubernetes ne connaît pas : version de politique, fallback, cache age, temps du décideur et validité des features.

## Limites à documenter

Le plugin ne résout pas à lui seul les limites du testbed. Dans kind, plusieurs nœuds Kubernetes partagent la même machine physique. Les métriques réseau/disque par nœud peuvent donc rester peu interprétables, même avec un plugin parfait.

Le plugin rend la mesure de l'intégration plus propre, mais il rend le développement plus dépendant de la version Kubernetes. Il faudra pinner la version du scheduler, les images, le binaire compilé et la configuration. Les résultats doivent toujours mentionner `integration=plugin` et la version Kubernetes.

## Critère de décision après pilote

Après le pilote, garder les deux chemins seulement si chacun apporte quelque chose :

- l'extender pour itération ML/LLM rapide ;
- le plugin pour mesures de latence/overhead plus proches du scheduler ;
- les deux pour une comparaison scientifique de l'intégration.

Si le plugin ne réduit pas le coût ou complique trop l'expérience, il peut rester une expérience secondaire. Si l'extender devient instable ou trop coûteux, le plugin devient le chemin principal pour les résultats finaux.
