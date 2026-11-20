# Generalization scorecard

run: 2026-11-20 00:05:36  |  pymolt: `.venv/bin/pymolt`

| toy | A recall | B hit | C rewrites ok/prop | D equiv | D change | D exact | E found (C-lvl) | F incon/imp.err | steps equiv / change (pymolt/agent/person) | secs |
|---|---|---|---|---|---|---|---|---|---|---|
| click | 2/3 (67%) | 100% | 0/0 | pass | fail | 63% | 0/0 (0/0) | 0/0 | 7 (0/3/4) / 7 (0/3/4) | 15.1 |
| django | 7/7 (100%) | 0% | 4/5 | inconclusive | fail | 76% | 0/0 (0/0) | 0/0 | 10 (3/5/2) / 10 (3/5/2) | 23.299999999999997 |
| jinja2 | 4/5 (80%) | 0% | 0/0 | inconclusive | fail | 68% | 0/0 (0/0) | 0/0 | 14 (0/12/2) / 14 (0/12/2) | 30.9 |
| networkx | 7/7 (100%) | 0% | 0/0 | inconclusive | fail | 68% | 1/5 (0/1) | 0/0 | 9 (1/6/2) / 9 (1/6/2) | 32.1 |
| pandas | 2/2 (100%) | 100% | 1/1 | pass | fail | 100% | 2/2 (0/0) | 0/0 | 6 (1/0/5) / 7 (1/0/6) | 29.299999999999997 |
| pydantic | 9/9 (100%) | 100% | 0/0 | inconclusive | fail | 87% | 0/0 (0/0) | 0/0 | 14 (0/10/4) / 15 (0/10/5) | 21.5 |
| pyyaml | 4/4 (100%) | 100% | 0/0 | inconclusive | fail | 78% | 1/2 (0/0) | 0/0 | 5 (0/2/3) / 5 (0/2/3) | 15.0 |
| sqlalchemy | 8/9 (89%) | 100% | 0/0 | inconclusive | fail | 51% | 0/0 (0/0) | 0/0 | 24 (0/22/2) / 24 (0/22/2) | 50.5 |
| stdlib | 10/11 (91%) | 0% | 2/2 | inconclusive | fail | 19% | 1/9 (0/3) | 0/0 | 19 (2/13/4) / 19 (2/13/4) | 30.7 |
| urllib3 | 5/5 (100%) | 0% | 0/0 | fail (exp not fail) | fail | 93% | 4/4 (0/0) | 0/0 | 9 (0/5/4) / 9 (0/5/4) | 18.9 |

## Skew signals

- click: A recall 67% is well below pandas 100%
- click: D exact share 63% is well below pandas 100%
- django: B hit rate 0% is well below pandas 100%
- django: D exact share 76% is well below pandas 100%
- jinja2: B hit rate 0% is well below pandas 100%
- jinja2: D exact share 68% is well below pandas 100%
- networkx: B hit rate 0% is well below pandas 100%
- networkx: D exact share 68% is well below pandas 100%
- networkx: 55% of returned values are opaque (type-only) vs 11% for pandas (S1)
- networkx: C-level warnings found 0/1 (S2)
- pyyaml: D exact share 78% is well below pandas 100%
- sqlalchemy: D exact share 51% is well below pandas 100%
- stdlib: B hit rate 0% is well below pandas 100%
- stdlib: D exact share 19% is well below pandas 100%
- stdlib: 80% of returned values are opaque (type-only) vs 11% for pandas (S1)
- stdlib: C-level warnings found 0/3 (S2)
- urllib3: B hit rate 0% is well below pandas 100%
- urllib3: verdict differs from expected in equiv

## Per toy detail

### click (cli, old python 3.11)
- status: ok
- A click.get_terminal_size REMOVED: detected, successor wrong/absent
- A click.argument SIGNATURE: missed, successor wrong/absent
- A click.MultiCommand DEPRECATED: detected, successor ok
- A click.testing.CliRunner.invoke BEHAVIOR: other_kind (plan kind DEFAULT)
- A successors correct 1/3
- A fp candidates (6): click.testing.CliRunner SIGNATURE; click.testing.CliRunner.__init__ SIGNATURE; click.types.Choice SIGNATURE; click.testing.CliRunner DEFAULT; click.testing.CliRunner.__init__ DEFAULT; click.testing.CliRunner.invoke DEFAULT
- B CliRunner.invoke: tier medium, calibration hit
- B calibration totals {'predicted': 10, 'hit': 1, 'prevented': 2, 'miss': 7, 'not_exercised': 0}; unpredicted changes []
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 1: ['click.testing.CliRunner.invoke']
- C tests after apply (equiv): 6 passed in 0.04s; attribution suite
- D equiv 12 edited sites: 12 equal, 0 representation-only, 0 need review, 0 unverified
- D equiv: pass (expected not fail), counts {'rewritten': 9, 'drift': 15, 'equal': 17}, missing changed [], detected by {}
- D change 12 edited sites: 12 equal, 0 representation-only, 0 need review, 0 unverified
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 9, 'drift': 15, 'equal': 18}, missing changed [], detected by {'CliRunner.invoke': 'changed'}
- D levels {'exact': 32, 'type': 19}, opaque share 0%
- C expected rewrites 2, proposed 0
- F delta sides ['click new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/click/change/.pymolt/cache/envs/cd7102e826d6591c)', 'click new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/click/equiv/.pymolt/cache/envs/cd7102e826d6591c)', 'click old: baseline capture interpreter (bench/generalization/.envs/click/old/bin/python)']
- F inconclusive [], partial [], import errors []

### django (web framework (settings at import time), old python 3.9)
- status: ok
- A django.conf.urls.url REMOVED: detected, successor wrong/absent
- A django.utils.six REMOVED: detected, successor wrong/absent
- A django.utils.encoding.force_text MOVED: detected, successor ok
- A django.utils.http.urlquote REMOVED: detected, successor wrong/absent
- A django.utils.translation.ugettext MOVED: detected, successor ok
- A django.utils.translation.ugettext_lazy MOVED: detected, successor ok
- A django.http.HttpRequest.is_ajax REMOVED: detected, successor wrong/absent
- A django.utils.html.escape BEHAVIOR: missed
- A successors correct 3/7
- B django.utils.html.escape: tier None, calibration None
- B calibration totals {'predicted': 18, 'hit': 0, 'prevented': 4, 'miss': 0, 'not_exercised': 14}; unpredicted changes ['change: django.utils.functional.wrapper']
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 0: ['django.utils.html.escape']
- C tests after apply (equiv): 5 passed in 0.04s; attribution per-rewrite: 4 correct, 0 wrong, 1 unknown of 5 rewrites
  - C django.utils.translation.gettext at app/core.py:7: unknown
- D equiv 14 edited sites: 4 equal, 0 representation-only, 0 need review, 10 unverified
- D equiv: inconclusive (expected not fail), counts {'rewritten': 14, 'drift': 5, 'equal': 8}, missing changed [], detected by {}
- D change 14 edited sites: 4 equal, 0 representation-only, 0 need review, 10 unverified
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 14, 'drift': 5, 'equal': 8}, missing changed [], detected by {'django.utils.html.escape': 'changed'}
- D levels {'exact': 13, 'type': 4}, opaque share 6%
- C expected rewrites 4, proposed 7
- F delta sides ['asgiref new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/django/change/.pymolt/cache/envs/653bda3768faee21)', 'asgiref new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/django/equiv/.pymolt/cache/envs/653bda3768faee21)', 'django new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/django/change/.pymolt/cache/envs/653bda3768faee21)', 'django new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/django/equiv/.pymolt/cache/envs/653bda3768faee21)', 'django old: baseline capture interpreter (bench/generalization/.envs/django/old/bin/python)', 'sqlparse new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/django/change/.pymolt/cache/envs/653bda3768faee21)', 'sqlparse new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/django/equiv/.pymolt/cache/envs/653bda3768faee21)', 'sqlparse old: baseline capture interpreter (bench/generalization/.envs/django/old/bin/python)']
- F inconclusive [], partial [], import errors []

### jinja2 (templating, old python 3.9)
- status: ok
- A jinja2.Markup MOVED: detected, successor ok
- A jinja2.escape MOVED: detected, successor ok
- A jinja2.contextfunction REMOVED: detected, successor wrong/absent
- A jinja2.evalcontextfilter REMOVED: detected, successor wrong/absent
- A jinja2.ext.with_ REMOVED: missed
- A jinja2.filters.do_urlize BEHAVIOR: missed
- A successors correct 2/4
- A fp candidates (8): jinja2.Markup.__add__ MOVED; jinja2.Markup.__mod__ MOVED; jinja2.Markup.__radd__ MOVED; jinja2.Markup.join MOVED; markupsafe._speedups.escape MOVED; markupsafe.Markup.__add__ SIGNATURE; markupsafe.Markup.__mod__ SIGNATURE; markupsafe.Markup.join SIGNATURE
- B Template.render: tier None, calibration None
- B calibration totals {'predicted': 42, 'hit': 0, 'prevented': 11, 'miss': 24, 'not_exercised': 7}; unpredicted changes ['change: jinja2.environment.Template.render']
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 0: ['jinja2.filters.do_urlize']
- C tests after apply (equiv): 5 passed in 0.02s; attribution suite
- D equiv 23 edited sites: 15 equal, 0 representation-only, 0 need review, 8 unverified
- D equiv: inconclusive (expected not fail), counts {'rewritten': 21, 'drift': 5, 'equal': 7}, missing changed [], detected by {}
- D change 22 edited sites: 16 equal, 0 representation-only, 0 need review, 6 unverified
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 20, 'drift': 6, 'equal': 7}, missing changed [], detected by {'Template.render': 'changed'}
- D levels {'exact': 28, 'type': 13}, opaque share 11%
- C expected rewrites 11, proposed 0
- F delta sides ['jinja2 new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/jinja2/change/.pymolt/cache/envs/695db8ae85b62cf6)', 'jinja2 new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/jinja2/equiv/.pymolt/cache/envs/695db8ae85b62cf6)', 'jinja2 old: baseline capture interpreter (bench/generalization/.envs/jinja2/old/bin/python)', 'markupsafe new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/jinja2/change/.pymolt/cache/envs/695db8ae85b62cf6)', 'markupsafe new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/jinja2/equiv/.pymolt/cache/envs/695db8ae85b62cf6)', 'markupsafe old: baseline capture interpreter (bench/generalization/.envs/jinja2/old/bin/python)']
- F inconclusive [], partial [], import errors []

### networkx (graphs, old python 3.8)
- status: ok
- A networkx.Graph.node MOVED: detected, successor ok
- A networkx.info REMOVED: detected, successor wrong/absent
- A networkx.to_numpy_matrix REMOVED: detected, successor wrong/absent
- A numpy.matrix.__mul__ BEHAVIOR: missed, successor wrong/absent
- A numpy.matrix.A1 BEHAVIOR: missed, successor wrong/absent
- A networkx.Graph.selfloop_edges REMOVED: detected, successor ok
- A networkx.connected_component_subgraphs REMOVED: detected, successor wrong/absent
- A networkx.write_gpickle REMOVED: detected, successor wrong/absent
- A networkx.read_gpickle REMOVED: detected, successor wrong/absent
- A successors correct 2/7
- A fp candidates (2): networkx.classes.reportviews.OutEdgeView.__call__ SIGNATURE; networkx.readwrite.json_graph.node_link.node_link_data SIGNATURE
- B networkx.node_link_data: tier None, calibration None
- B calibration totals {'predicted': 14, 'hit': 0, 'prevented': 2, 'miss': 2, 'not_exercised': 10}; unpredicted changes ['change: json.dumps', 'change: networkx.readwrite.json_graph.node_link.node_link_data', 'change: json.loads']
- A BEHAVIOR entries left out of recall (verify's): 2, seen by the plan 0: ['numpy.matrix.__mul__', 'numpy.matrix.A1']
- C tests after apply (equiv): 9 passed in 0.39s; attribution suite
- D equiv 32 edited sites: 4 equal, 1 representation-only, 0 need review, 27 unverified
- D equiv: inconclusive (expected not fail), counts {'rewritten': 31, 'drift': 6, 'equal': 17}, missing changed [], detected by {}
- D change 32 edited sites: 4 equal, 1 representation-only, 0 need review, 27 unverified
- D change: fail (expected fail), counts {'changed': 3, 'rewritten': 31, 'drift': 6, 'equal': 17}, missing changed [], detected by {'networkx.node_link_data': 'changed'}
- D levels {'exact': 13, 'type': 6}, opaque share 55%
- C expected rewrites 1, proposed 0
- F delta sides ['networkx new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/networkx/change/.pymolt/cache/envs/6e204b673b907f68)', 'networkx new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/networkx/equiv/.pymolt/cache/envs/6e204b673b907f68)', 'networkx old: baseline capture interpreter (bench/generalization/.envs/networkx/old/bin/python)']
- E missed: ['numpy.matrix PendingDeprecationWarning', 'networkx.connected_component_subgraphs DeprecationWarning', 'numpy.int (alias, networkx.readwrite import time) DeprecationWarning', 'networkx.convert_matrix / drawing.nx_pydot ("is" with a literal) SyntaxWarning']
- F inconclusive [], partial [], import errors []

### pandas (dataframes (reference), old python 3.11)
- status: ok
- A pandas.core.series.Series.append REMOVED: detected, successor ok
- A pandas.core.frame.DataFrame.mean DEFAULT: detected
- A successors correct 1/1
- A fp candidates (6): pandas.core.frame.DataFrame.groupby SIGNATURE; pandas.core.series.Series.to_dict SIGNATURE; pandas.core.frame.DataFrame.groupby DEFAULT; pandas.core.groupby.groupby.GroupBy.sum DEFAULT; pandas.core.series.Series DEFAULT; pandas.core.series.Series.__init__ DEFAULT
- B pandas.core.frame.DataFrame.mean: tier high, calibration hit
- B calibration totals {'predicted': 11, 'hit': 1, 'prevented': 2, 'miss': 8, 'not_exercised': 0}; unpredicted changes []
- C tests after apply (equiv): 3 passed in 0.62s; attribution per-rewrite: 1 correct, 0 wrong, 0 unknown of 1 rewrites
- D equiv 1 edited sites: 1 equal, 0 representation-only, 0 need review, 0 unverified
- D equiv: pass (expected not fail), counts {'rewritten': 1, 'drift': 2, 'equal': 9}, missing changed [], detected by {}
- D change 1 edited sites: 1 equal, 0 representation-only, 0 need review, 0 unverified
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 1, 'disappeared': 1, 'drift': 2, 'equal': 10}, missing changed [], detected by {'pandas.core.frame.DataFrame.mean': 'changed'}
- D levels {'exact': 7, 'type': 0}, opaque share 11%
- C expected rewrites 1, proposed 1
- F delta sides ['numpy new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pandas/change/.pymolt/cache/envs/7a4e5cb741884e8d)', 'numpy new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pandas/equiv/.pymolt/cache/envs/7a4e5cb741884e8d)', 'numpy old: baseline capture interpreter (bench/generalization/.envs/pandas/old/bin/python)', 'pandas new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pandas/change/.pymolt/cache/envs/7a4e5cb741884e8d)', 'pandas new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pandas/equiv/.pymolt/cache/envs/7a4e5cb741884e8d)', 'pandas old: baseline capture interpreter (bench/generalization/.envs/pandas/old/bin/python)']
- F inconclusive [], partial [], import errors []

### pydantic (validation / web, old python 3.11)
- status: ok
- A pydantic.Field SIGNATURE: detected, successor wrong/absent
- A pydantic.validator DEPRECATED: detected, successor ok
- A pydantic.BaseModel.dict DEPRECATED: detected, successor ok
- A pydantic.BaseModel.json DEPRECATED: detected, successor ok
- A pydantic.BaseModel.json BEHAVIOR: other_kind (plan kind DEFAULT), successor wrong/absent
- A pydantic.BaseModel.parse_obj DEPRECATED: detected, successor ok
- A pydantic.BaseModel.parse_raw DEPRECATED: detected, successor ok
- A pydantic.BaseModel.copy DEPRECATED: detected, successor ok
- A pydantic.BaseModel.schema DEPRECATED: detected, successor ok
- A pydantic.BaseModel.__fields__ DEPRECATED: detected, successor ok
- A pydantic.BaseModel.Config BEHAVIOR: missed, successor wrong/absent
- A pydantic.BaseModel BEHAVIOR: other_kind (plan kind SIGNATURE), successor wrong/absent
- A pydantic.BaseModel.parse_obj BEHAVIOR: other_kind (plan kind DEPRECATED)
- A successors correct 8/9
- A fp candidates (13): pydantic.utils.in_ipython REMOVED; pydantic.class_validators.Validator.__init__ MOVED; pydantic.main.ModelMetaclass.__new__ MOVED; pydantic.error_wrappers.ValidationError SIGNATURE; pydantic.main.dict SIGNATURE; pydantic.main.json SIGNATURE; pydantic.main.json DEFAULT; pydantic.main.dict DEPRECATED
- B pydantic.main.BaseModel.parse_obj: tier low, calibration hit
- B pydantic.main.BaseModel.schema: tier high, calibration hit
- B calibration totals {'predicted': 43, 'hit': 2, 'prevented': 2, 'miss': 19, 'not_exercised': 20}; unpredicted changes []
- A BEHAVIOR entries left out of recall (verify's): 4, seen by the plan 3: ['pydantic.BaseModel.json', 'pydantic.BaseModel.Config', 'pydantic.BaseModel', 'pydantic.BaseModel.parse_obj']
- C tests after apply (equiv): 7 passed, 12 warnings in 0.05s; attribution suite
- D equiv 21 edited sites: 8 equal, 0 representation-only, 0 need review, 13 unverified
- D equiv: inconclusive (expected not fail), counts {'rewritten': 16, 'drift': 20, 'equal': 6}, missing changed [], detected by {}
- D change 21 edited sites: 8 equal, 0 representation-only, 0 need review, 13 unverified
- D change: fail (expected fail), counts {'changed': 2, 'rewritten': 16, 'drift': 25, 'equal': 7}, missing changed [], detected by {'pydantic.main.BaseModel.parse_obj': 'changed', 'pydantic.main.BaseModel.schema': 'changed'}
- D levels {'exact': 13, 'type': 2}, opaque share 10%
- C expected rewrites 8, proposed 0
- F delta sides ['pydantic new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pydantic/change/.pymolt/cache/envs/d89d04d9b8b54841)', 'pydantic new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pydantic/equiv/.pymolt/cache/envs/d89d04d9b8b54841)', 'pydantic old: baseline capture interpreter (bench/generalization/.envs/pydantic/old/bin/python)', 'pydantic-core new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pydantic/change/.pymolt/cache/envs/d89d04d9b8b54841)', 'pydantic-core new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pydantic/equiv/.pymolt/cache/envs/d89d04d9b8b54841)']
- F inconclusive [], partial [], import errors []

### pyyaml (serialization, old python 3.11)
- status: ok
- A yaml.load SIGNATURE: detected
- A yaml.load_all SIGNATURE: detected
- A yaml.YAMLLoadWarning REMOVED: detected
- A yaml.load SIGNATURE: detected
- A successors correct 0/0
- B yaml.load: tier high, calibration hit
- B calibration totals {'predicted': 9, 'hit': 1, 'prevented': 0, 'miss': 6, 'not_exercised': 2}; unpredicted changes []
- C tests after apply (equiv): 6 passed in 0.02s; attribution suite
- D equiv 3 edited sites: 2 equal, 0 representation-only, 0 need review, 1 unverified
- D equiv: inconclusive (expected not fail), counts {'rewritten': 3, 'drift': 1, 'equal': 6}, missing changed [], detected by {}
- D change 3 edited sites: 2 equal, 0 representation-only, 0 need review, 1 unverified
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 3, 'drift': 1, 'equal': 6}, missing changed [], detected by {'yaml.load': 'changed'}
- D levels {'exact': 7, 'type': 2}, opaque share 11%
- C expected rewrites 0, proposed 0
- F delta sides ['pyyaml new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pyyaml/change/.pymolt/cache/envs/1edcb82c6d2656b1)', 'pyyaml new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/pyyaml/equiv/.pymolt/cache/envs/1edcb82c6d2656b1)', 'pyyaml old: baseline capture interpreter (bench/generalization/.envs/pyyaml/old/bin/python)']
- E missed: ['yaml.load_all YAMLLoadWarning']
- F inconclusive [], partial [], import errors []

### sqlalchemy (ORM / DB (sqlite in-memory), old python 3.9)
- status: ok
- A sqlalchemy.engine.Engine.execute REMOVED: detected, successor wrong/absent
- A sqlalchemy.select SIGNATURE: detected, successor wrong/absent
- A sqlalchemy.engine.RowProxy.__getitem__ REMOVED: missed, successor wrong/absent
- A sqlalchemy.engine.RowProxy BEHAVIOR: missed, successor wrong/absent
- A sqlalchemy.engine.Connection.execute SIGNATURE: detected, successor wrong/absent
- A sqlalchemy.engine.Engine.table_names REMOVED: detected, successor wrong/absent
- A sqlalchemy.MetaData SIGNATURE: detected, successor wrong/absent
- A sqlalchemy.ext.declarative.declarative_base DEPRECATED: detected, successor wrong/absent
- A sqlalchemy.orm.Query.get DEPRECATED: detected, successor wrong/absent
- A sqlalchemy.engine.RowProxy.keys REMOVED: detected, successor wrong/absent
- A successors correct 0/9
- A fp candidates (21): sqlalchemy.engine.result.ResultProxy.fetchall REMOVED; sqlalchemy.engine.result.ResultProxy.first REMOVED; sqlalchemy.engine.result.ResultProxy.scalar REMOVED; sqlalchemy.engine.result.RowProxy.__eq__ REMOVED; sqlalchemy.engine.result.RowProxy.keys REMOVED; sqlalchemy.ext.declarative.api.DeclarativeMeta.__init__ REMOVED; sqlalchemy.engine.base.Engine.connect SIGNATURE; sqlalchemy.engine.create_engine SIGNATURE
- B sqlalchemy.engine.result.RowProxy.keys: tier certain, calibration hit
- B calibration totals {'predicted': 61, 'hit': 1, 'prevented': 10, 'miss': 22, 'not_exercised': 28}; unpredicted changes []
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 0: ['sqlalchemy.engine.RowProxy']
- C tests after apply (equiv): 6 passed in 0.43s; attribution suite
- D equiv 66 edited sites: 20 equal, 0 representation-only, 3 need review, 43 unverified
- D equiv: inconclusive (expected not fail), counts {'rewritten': 63, 'drift': 25, 'equal': 13}, missing changed [], detected by {}
- D change 66 edited sites: 20 equal, 0 representation-only, 3 need review, 43 unverified
- D change: fail (expected fail), counts {'rewritten': 63, 'disappeared': 1, 'appeared': 1, 'drift': 25, 'equal': 13}, missing changed [], detected by {'sqlalchemy.engine.result.RowProxy.keys': 'disappeared'}
- D levels {'exact': 48, 'type': 46}, opaque share 4%
- C expected rewrites 3, proposed 0
- F delta sides ['sqlalchemy new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/sqlalchemy/change/.pymolt/cache/envs/8e304b5439d46c2d)', 'sqlalchemy new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/sqlalchemy/equiv/.pymolt/cache/envs/8e304b5439d46c2d)', 'sqlalchemy old: baseline capture interpreter (bench/generalization/.envs/sqlalchemy/old/bin/python)']
- F inconclusive [], partial [], import errors []

### stdlib (python-stdlib, old python 3.8)
- status: ok
- A asyncio.sleep SIGNATURE: detected
- A asyncio.Queue SIGNATURE: detected
- A imp.reload REMOVED: detected, successor wrong/absent
- A distutils.version.LooseVersion REMOVED: detected
- A collections.Mapping MOVED: detected, successor ok
- A unittest.TestCase.assertEquals REMOVED: detected, successor wrong/absent
- A inspect.getargspec REMOVED: detected, successor wrong/absent
- A inspect.ArgSpec.keywords REMOVED: missed, successor wrong/absent
- A xml.etree.ElementTree.Element.getchildren REMOVED: detected, successor wrong/absent
- A datetime.datetime.utcnow DEPRECATED: detected, successor wrong/absent
- A datetime.datetime.utcfromtimestamp DEPRECATED: detected, successor wrong/absent
- A successors correct 1/8
- B datetime.datetime.fromisoformat: tier None, calibration None
- B calibration totals {'predicted': 50, 'hit': 0, 'prevented': 2, 'miss': 14, 'not_exercised': 34}; unpredicted changes ['change: datetime.datetime.fromisoformat']
- C tests after apply (equiv): 10 passed, 1 warning in 0.16s; attribution per-rewrite: 2 correct, 0 wrong, 0 unknown of 2 rewrites
- D equiv 18 edited sites: 8 equal, 0 representation-only, 0 need review, 10 unverified
- D equiv: inconclusive (expected not fail), counts {'rewritten': 27, 'drift': 1, 'equal': 17}, missing changed [], detected by {}
- D change 18 edited sites: 8 equal, 0 representation-only, 0 need review, 10 unverified
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 27, 'drift': 1, 'equal': 17}, missing changed [], detected by {'datetime.datetime.fromisoformat': 'changed'}
- D levels {'exact': 4, 'type': 17}, opaque share 80%
- E post-migration (new side) datetime.datetime.utcnow DeprecationWarning [C-level]: not observed
- E post-migration (new side) datetime.datetime.utcfromtimestamp DeprecationWarning [C-level]: not observed
- E post-migration (new side) re.findall (non-raw '\d+' literal) SyntaxWarning [C-level]: not observed
- E post-migration (new side) builtins.open (unclosed file) ResourceWarning [C-level]: not observed
- C expected rewrites 5, proposed 4
- F delta sides ['iniconfig new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/stdlib/change/.pymolt/cache/envs/67cd507464ddb3e4)', 'iniconfig new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/stdlib/equiv/.pymolt/cache/envs/67cd507464ddb3e4)', 'iniconfig old: baseline capture interpreter (bench/generalization/.envs/stdlib/old/bin/python)', 'packaging new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/stdlib/change/.pymolt/cache/envs/67cd507464ddb3e4)', 'packaging new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/stdlib/equiv/.pymolt/cache/envs/67cd507464ddb3e4)', 'packaging old: baseline capture interpreter (bench/generalization/.envs/stdlib/old/bin/python)', 'pluggy new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/stdlib/change/.pymolt/cache/envs/67cd507464ddb3e4)', 'pluggy new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/stdlib/equiv/.pymolt/cache/envs/67cd507464ddb3e4)', 'pluggy old: baseline capture interpreter (bench/generalization/.envs/stdlib/old/bin/python)', 'pytest new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/stdlib/change/.pymolt/cache/envs/67cd507464ddb3e4)', 'pytest new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/stdlib/equiv/.pymolt/cache/envs/67cd507464ddb3e4)', 'pytest old: baseline capture interpreter (bench/generalization/.envs/stdlib/old/bin/python)']
- E missed: ['asyncio.sleep DeprecationWarning', 'imp DeprecationWarning', 'collections.Mapping DeprecationWarning', 'unittest.TestCase.assertEquals DeprecationWarning', 'inspect.getargspec DeprecationWarning', 'xml.etree.ElementTree.Element.getchildren DeprecationWarning', "re.findall (non-raw '\\d+' literal in app/xmlio.py) DeprecationWarning", 'builtins.open (unclosed file in xmlio.read_all) ResourceWarning']
- F inconclusive [], partial [], import errors []

### urllib3 (http-client (offline), old python 3.11)
- status: ok
- A urllib3.util.retry.Retry SIGNATURE: detected, successor wrong/absent
- A urllib3.util.retry.Retry.DEFAULT_METHOD_WHITELIST MOVED: detected, successor ok
- A urllib3.response.HTTPResponse.getheader DEPRECATED: detected, successor ok
- A urllib3.response.HTTPResponse.getheaders DEPRECATED: detected, successor wrong/absent
- A urllib3.get_host REMOVED: detected, successor ok
- A successors correct 3/5
- A fp candidates (6): urllib3.response.HTTPResponse SIGNATURE; urllib3.response.HTTPResponse.__init__ SIGNATURE; urllib3.response.HTTPResponse DEFAULT; urllib3.response.HTTPResponse.__init__ DEFAULT; urllib3.util.timeout.Timeout DEFAULT; urllib3.util.timeout.Timeout.__init__ DEFAULT
- B urllib3.response.HTTPResponse.stream: tier None, calibration None
- B calibration totals {'predicted': 15, 'hit': 2, 'prevented': 2, 'miss': 5, 'not_exercised': 6}; unpredicted changes ['change: gzip.compress']
- C tests after apply (equiv): 7 passed in 0.02s; attribution suite
- D equiv 8 edited sites: 2 equal, 1 representation-only, 0 need review, 4 unverified, 1 failed
- D equiv: fail (expected not fail), counts {'rewritten': 6, 'drift': 4, 'equal': 9}, missing changed [], detected by {}
- D change 8 edited sites: 2 equal, 1 representation-only, 0 need review, 4 unverified, 1 failed
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 6, 'drift': 5, 'equal': 11}, missing changed ['urllib3.response.HTTPResponse.stream'], detected by {}
- D levels {'exact': 14, 'type': 1}, opaque share 9%
- C expected rewrites 2, proposed 0
- F delta sides ['urllib3 new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/urllib3/change/.pymolt/cache/envs/090e85a53af8b8c4)', 'urllib3 new: throwaway environment from requirements-target.txt (bench/generalization/results/release-0.2-record/urllib3/equiv/.pymolt/cache/envs/090e85a53af8b8c4)', 'urllib3 old: baseline capture interpreter (bench/generalization/.envs/urllib3/old/bin/python)']
- F inconclusive [], partial [], import errors []

