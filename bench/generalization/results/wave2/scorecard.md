# Generalization scorecard

run: 2026-11-15 20:14:05  |  pymolt: `.venv/bin/pymolt`

| toy | A recall | B hit | C rewrites ok/prop | D equiv | D change | D exact | E found (C-lvl) | F incon/imp.err | secs |
|---|---|---|---|---|---|---|---|---|---|
| click | 2/3 (67%) | 0% | 0/0 | fail (exp pass) | fail | 100% | 0/0 (0/0) | 0/0 | 13.3 |
| django | 4/7 (57%) | 0% | 0/2 | inconclusive (exp pass) | inconclusive (exp fail) | - | 0/0 (0/0) | 0/0 | 16.6 |
| jinja2 | 2/5 (40%) | 0% | 0/0 | inconclusive (exp pass) | inconclusive (exp fail) | 100% | 0/0 (0/0) | 0/0 | 14.8 |
| networkx | 6/8 (75%) | 0% | 0/0 | fail (exp pass) | fail | 78% | 1/5 (0/1) | 0/0 | 27.9 |
| pandas | 2/2 (100%) | 100% | 1/1 | pass | fail | 60% | 2/2 (0/0) | 0/0 | 28.9 |
| pydantic | 0/9 (0%) | 0% | 0/0 | inconclusive (exp pass) | inconclusive (exp fail) | - | 0/0 (0/0) | 0/0 | 16.5 |
| pyyaml | 1/4 (25%) | 100% | 0/0 | pass | fail | 75% | 0/2 (0/0) | 0/0 | 13.3 |
| sqlalchemy | 7/9 (78%) | 100% | 0/0 | fail (exp pass) | fail | 21% | 0/0 (0/0) | 0/0 | 19.9 |
| stdlib | 5/11 (45%) | 0% | 4/4 | fail (exp pass) | fail | - | 0/9 (0/3) | 0/0 | 14.7 |
| urllib3 | 5/5 (100%) | 0% | 0/0 | fail (exp pass) | fail | 70% | 4/4 (0/0) | 0/0 | 12.7 |

## Skew signals

- click: A recall 67% is well below pandas 100%
- click: B hit rate 0% is well below pandas 100%
- click: verdict differs from expected in equiv
- django: A recall 57% is well below pandas 100%
- django: B hit rate 0% is well below pandas 100%
- django: verdict differs from expected in equiv, change
- django: 2 rewrite(s) left the equiv tests red
- django: step failures: equiv:capture_post, change:capture_post
- jinja2: A recall 40% is well below pandas 100%
- jinja2: B hit rate 0% is well below pandas 100%
- jinja2: verdict differs from expected in equiv, change
- networkx: A recall 75% is well below pandas 100%
- networkx: B hit rate 0% is well below pandas 100%
- networkx: verdict differs from expected in equiv
- networkx: C-level warnings found 0/1 (S2)
- pydantic: A recall 0% is well below pandas 100%
- pydantic: B hit rate 0% is well below pandas 100%
- pydantic: verdict differs from expected in equiv, change
- pydantic: step failures: equiv:capture_baseline, equiv:apply, change:capture_baseline, change:apply
- pyyaml: A recall 25% is well below pandas 100%
- sqlalchemy: A recall 78% is well below pandas 100%
- sqlalchemy: D exact share 21% is well below pandas 60%
- sqlalchemy: 75% of returned values are opaque (type-only) vs 45% for pandas (S1)
- sqlalchemy: verdict differs from expected in equiv
- stdlib: A recall 45% is well below pandas 100%
- stdlib: B hit rate 0% is well below pandas 100%
- stdlib: verdict differs from expected in equiv
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
- A fp candidates (7): click.testing.CliRunner SIGNATURE; click.testing.CliRunner.__init__ SIGNATURE; click.types.Choice SIGNATURE; click.testing.CliRunner DEFAULT; click.testing.CliRunner.__init__ DEFAULT; click.testing.CliRunner.invoke DEFAULT; click.core.MultiCommand.__init__ DEPRECATED
- B CliRunner.invoke: tier medium, calibration miss
- B calibration totals {'predicted': 10, 'hit': 2, 'prevented': 0, 'miss': 8, 'not_exercised': 0}; unpredicted changes []
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 1: ['click.testing.CliRunner.invoke']
- C tests after apply (equiv): 6 passed in 0.04s; attribution suite
- D equiv: fail (expected pass), counts {'disappeared': 1, 'drift': 2, 'equal': 11}, missing changed [], detected by {}
- D change: fail (expected fail), counts {'disappeared': 1, 'drift': 2, 'equal': 12}, missing changed ['CliRunner.invoke'], detected by {}
- D levels {'exact': 4, 'type': 0}, opaque share 29%
- C expected rewrites 2, proposed 0
- F delta sides ['click new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/click/change/.pymolt/cache/envs/cd7102e826d6591c)', 'click new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/click/equiv/.pymolt/cache/envs/cd7102e826d6591c)', 'click old: baseline capture interpreter (bench/generalization/.envs/click/old/bin/python)']
- F inconclusive [], partial [], import errors []

### django (web framework (settings at import time), old python 3.9)
- status: step failures: equiv:capture_post, change:capture_post
- A django.conf.urls.url REMOVED: detected, successor wrong/absent
- A django.utils.six REMOVED: missed, successor wrong/absent
- A django.utils.encoding.force_text REMOVED: detected, successor wrong/absent
- A django.utils.http.urlquote REMOVED: detected, successor wrong/absent
- A django.utils.translation.ugettext REMOVED: other_kind (plan kind MOVED), successor ok
- A django.utils.translation.ugettext_lazy REMOVED: other_kind (plan kind MOVED), successor wrong/absent
- A django.http.HttpRequest.is_ajax REMOVED: detected, successor wrong/absent
- A django.utils.html.escape BEHAVIOR: missed
- A successors correct 1/7
- A fp candidates (1): django.utils.six.string_types REMOVED
- B django.utils.html.escape: tier None, calibration None
- B calibration totals {'predicted': 0, 'hit': 0, 'prevented': 0, 'miss': 0, 'not_exercised': 0}; unpredicted changes []
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 0: ['django.utils.html.escape']
- C tests after apply (equiv): 1 error in 0.10s; attribution suite
- D equiv: inconclusive (expected pass), counts {}, missing changed [], detected by {}
- D change: inconclusive (expected fail), counts {}, missing changed ['django.utils.html.escape'], detected by {}
- D levels {'exact': 0, 'type': 0}, opaque share 24%
- C expected rewrites 4, proposed 2
- F delta sides ['asgiref new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/django/change/.pymolt/cache/envs/653bda3768faee21)', 'asgiref new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/django/equiv/.pymolt/cache/envs/653bda3768faee21)', 'asgiref old: throwaway environment pinned to the from-versions (bench/generalization/results/wave2/django/change/.pymolt/cache/envs/3b2df04512978d5d)', 'asgiref old: throwaway environment pinned to the from-versions (bench/generalization/results/wave2/django/equiv/.pymolt/cache/envs/3b2df04512978d5d)', 'django new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/django/change/.pymolt/cache/envs/653bda3768faee21)', 'django new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/django/equiv/.pymolt/cache/envs/653bda3768faee21)', 'django old: throwaway environment pinned to the from-versions (bench/generalization/results/wave2/django/change/.pymolt/cache/envs/3b2df04512978d5d)', 'django old: throwaway environment pinned to the from-versions (bench/generalization/results/wave2/django/equiv/.pymolt/cache/envs/3b2df04512978d5d)', 'sqlparse new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/django/change/.pymolt/cache/envs/653bda3768faee21)', 'sqlparse new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/django/equiv/.pymolt/cache/envs/653bda3768faee21)', 'sqlparse old: throwaway environment pinned to the from-versions (bench/generalization/results/wave2/django/change/.pymolt/cache/envs/3b2df04512978d5d)', 'sqlparse old: throwaway environment pinned to the from-versions (bench/generalization/results/wave2/django/equiv/.pymolt/cache/envs/3b2df04512978d5d)']
- F inconclusive [], partial [], import errors []
- runner warnings: ["manual_migration: app/core.py: old text not found: 'from django.utils import six\\nfrom django.utils.encoding impo'", "manual_migration: app/core.py: old text not found: 'from django.utils import six\\nfrom django.utils.encoding impo'"]

### jinja2 (templating, old python 3.9)
- status: ok
- A jinja2.Markup MOVED: detected, successor ok
- A jinja2.escape MOVED: detected, successor ok
- A jinja2.contextfunction MOVED: other_kind (plan kind REMOVED), successor wrong/absent
- A jinja2.evalcontextfilter MOVED: other_kind (plan kind REMOVED), successor wrong/absent
- A jinja2.ext.with_ REMOVED: missed
- A jinja2.filters.do_urlize BEHAVIOR: missed
- A successors correct 2/4
- A fp candidates (8): jinja2.Markup.__add__ MOVED; jinja2.Markup.__mod__ MOVED; jinja2.Markup.__radd__ MOVED; jinja2.Markup.join MOVED; markupsafe.Markup.__add__ SIGNATURE; markupsafe.Markup.__mod__ SIGNATURE; markupsafe.Markup.__new__ SIGNATURE; markupsafe.Markup.join SIGNATURE
- B Template.render: tier None, calibration None
- B calibration totals {'predicted': 42, 'hit': 0, 'prevented': 0, 'miss': 38, 'not_exercised': 4}; unpredicted changes []
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 0: ['jinja2.filters.do_urlize']
- C tests after apply (equiv): 5 passed in 0.10s; attribution suite
- D equiv: inconclusive (expected pass), counts {'rewritten': 15}, missing changed [], detected by {}
- D change: inconclusive (expected fail), counts {'rewritten': 15}, missing changed ['Template.render'], detected by {}
- D levels {'exact': 28, 'type': 0}, opaque share 18%
- C expected rewrites 11, proposed 0
- F delta sides ['jinja2 new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/jinja2/change/.pymolt/cache/envs/695db8ae85b62cf6)', 'jinja2 new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/jinja2/equiv/.pymolt/cache/envs/695db8ae85b62cf6)', 'jinja2 old: baseline capture interpreter (bench/generalization/.envs/jinja2/old/bin/python)', 'markupsafe new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/jinja2/change/.pymolt/cache/envs/695db8ae85b62cf6)', 'markupsafe new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/jinja2/equiv/.pymolt/cache/envs/695db8ae85b62cf6)', 'markupsafe old: baseline capture interpreter (bench/generalization/.envs/jinja2/old/bin/python)']
- F inconclusive [], partial [], import errors []

### networkx (graphs, old python 3.8)
- status: ok
- A networkx.Graph.node REMOVED: missed, successor wrong/absent
- A networkx.info REMOVED: detected, successor wrong/absent
- A networkx.to_numpy_matrix REMOVED: detected, successor wrong/absent
- A numpy.matrix.__mul__ BEHAVIOR: missed, successor wrong/absent
- A numpy.matrix.A1 REMOVED: missed, successor wrong/absent
- A networkx.Graph.selfloop_edges REMOVED: detected, successor ok
- A networkx.connected_component_subgraphs REMOVED: detected, successor wrong/absent
- A networkx.write_gpickle REMOVED: detected, successor wrong/absent
- A networkx.read_gpickle REMOVED: detected, successor wrong/absent
- A successors correct 1/8
- A fp candidates (2): networkx.classes.reportviews.OutEdgeView.__call__ SIGNATURE; networkx.readwrite.json_graph.node_link.node_link_data SIGNATURE
- B networkx.node_link_data: tier None, calibration None
- B calibration totals {'predicted': 14, 'hit': 6, 'prevented': 0, 'miss': 2, 'not_exercised': 6}; unpredicted changes ['change: networkx.readwrite.json_graph.node_link.node_link_data']
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 0: ['numpy.matrix.__mul__']
- C tests after apply (equiv): 9 passed in 0.57s; attribution suite
- D equiv: fail (expected pass), counts {'rewritten': 7, 'disappeared': 3, 'appeared': 3, 'equal': 9}, missing changed [], detected by {}
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 7, 'disappeared': 3, 'appeared': 3, 'equal': 9}, missing changed [], detected by {'networkx.node_link_data': 'changed'}
- D levels {'exact': 7, 'type': 2}, opaque share 58%
- C expected rewrites 1, proposed 0
- F delta sides ['networkx new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/networkx/change/.pymolt/cache/envs/6e204b673b907f68)', 'networkx new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/networkx/equiv/.pymolt/cache/envs/6e204b673b907f68)', 'networkx old: baseline capture interpreter (bench/generalization/.envs/networkx/old/bin/python)']
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
- C tests after apply (equiv): 3 passed in 0.87s; attribution per-rewrite: 1 correct, 0 wrong, 0 unknown of 1 rewrites
- D equiv: pass (expected pass), counts {'rewritten': 1, 'drift': 1, 'equal': 6}, missing changed [], detected by {}
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 1, 'disappeared': 1, 'drift': 1, 'equal': 7}, missing changed [], detected by {'pandas.core.frame.DataFrame.mean': 'changed'}
- D levels {'exact': 3, 'type': 2}, opaque share 45%
- C expected rewrites 1, proposed 1
- F delta sides ['numpy new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/pandas/change/.pymolt/cache/envs/7a4e5cb741884e8d)', 'numpy new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/pandas/equiv/.pymolt/cache/envs/7a4e5cb741884e8d)', 'numpy old: baseline capture interpreter (bench/generalization/.envs/pandas/old/bin/python)', 'pandas new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/pandas/change/.pymolt/cache/envs/7a4e5cb741884e8d)', 'pandas new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/pandas/equiv/.pymolt/cache/envs/7a4e5cb741884e8d)', 'pandas old: baseline capture interpreter (bench/generalization/.envs/pandas/old/bin/python)']
- F inconclusive [], partial [], import errors []

### pydantic (validation / web, old python 3.11)
- status: step failures: equiv:capture_baseline, equiv:apply, change:capture_baseline, change:apply
- A pydantic.Field SIGNATURE: missed, successor wrong/absent
- A pydantic.validator DEPRECATED: missed, successor wrong/absent
- A pydantic.BaseModel.dict DEPRECATED: missed, successor wrong/absent
- A pydantic.BaseModel.json DEPRECATED: missed, successor wrong/absent
- A pydantic.BaseModel.json BEHAVIOR: missed, successor wrong/absent
- A pydantic.BaseModel.parse_obj DEPRECATED: missed, successor wrong/absent
- A pydantic.BaseModel.parse_raw DEPRECATED: missed, successor wrong/absent
- A pydantic.BaseModel.copy DEPRECATED: missed, successor wrong/absent
- A pydantic.BaseModel.schema DEPRECATED: missed, successor wrong/absent
- A pydantic.BaseModel.__fields__ DEPRECATED: missed, successor wrong/absent
- A pydantic.BaseModel.Config BEHAVIOR: missed, successor wrong/absent
- A pydantic.BaseModel BEHAVIOR: missed, successor wrong/absent
- A pydantic.BaseModel.parse_obj BEHAVIOR: missed
- A successors correct 0/9
- B pydantic.main.BaseModel.parse_obj: tier None, calibration None
- B calibration totals {'predicted': 0, 'hit': 0, 'prevented': 0, 'miss': 0, 'not_exercised': 0}; unpredicted changes []
- A BEHAVIOR entries left out of recall (verify's): 4, seen by the plan 0: ['pydantic.BaseModel.json', 'pydantic.BaseModel.Config', 'pydantic.BaseModel', 'pydantic.BaseModel.parse_obj']
- C tests after apply (equiv): 7 passed, 13 warnings in 0.23s; attribution suite
- D equiv: inconclusive (expected pass), counts {}, missing changed [], detected by {}
- D change: inconclusive (expected fail), counts {}, missing changed ['pydantic.main.BaseModel.parse_obj'], detected by {}
- D levels {'exact': 0, 'type': 0}, opaque share -
- C expected rewrites 8, proposed 0
- F delta sides []
- F inconclusive [], partial [], import errors []

### pyyaml (serialization, old python 3.11)
- status: ok
- A yaml.load SIGNATURE: other_kind (plan kind DEFAULT)
- A yaml.load_all SIGNATURE: other_kind (plan kind DEFAULT)
- A yaml.YAMLLoadWarning REMOVED: detected
- A yaml.load SIGNATURE: other_kind (plan kind DEFAULT)
- A successors correct 0/0
- B yaml.load: tier medium, calibration hit
- B calibration totals {'predicted': 7, 'hit': 1, 'prevented': 0, 'miss': 4, 'not_exercised': 2}; unpredicted changes []
- C tests after apply (equiv): 6 passed in 0.08s; attribution suite
- D equiv: pass (expected pass), counts {'rewritten': 2, 'equal': 3}, missing changed [], detected by {}
- D change: fail (expected fail), counts {'rewritten': 2, 'disappeared': 1, 'equal': 3}, missing changed [], detected by {'yaml.load': 'disappeared'}
- D levels {'exact': 6, 'type': 2}, opaque share 11%
- C expected rewrites 0, proposed 0
- F delta sides ['pyyaml new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/pyyaml/change/.pymolt/cache/envs/1edcb82c6d2656b1)', 'pyyaml new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/pyyaml/equiv/.pymolt/cache/envs/1edcb82c6d2656b1)', 'pyyaml old: baseline capture interpreter (bench/generalization/.envs/pyyaml/old/bin/python)']
- E missed: ['yaml.load YAMLLoadWarning', 'yaml.load_all YAMLLoadWarning']
- F inconclusive [], partial [], import errors []

### sqlalchemy (ORM / DB (sqlite in-memory), old python 3.9)
- status: ok
- A sqlalchemy.engine.Engine.execute REMOVED: detected, successor wrong/absent
- A sqlalchemy.select SIGNATURE: other_kind (plan kind DEPRECATED), successor wrong/absent
- A sqlalchemy.engine.RowProxy.__getitem__ REMOVED: missed, successor wrong/absent
- A sqlalchemy.engine.RowProxy BEHAVIOR: missed, successor wrong/absent
- A sqlalchemy.engine.Connection.execute SIGNATURE: detected, successor wrong/absent
- A sqlalchemy.engine.Engine.table_names REMOVED: detected, successor wrong/absent
- A sqlalchemy.MetaData SIGNATURE: detected, successor wrong/absent
- A sqlalchemy.ext.declarative.declarative_base MOVED: detected, successor wrong/absent
- A sqlalchemy.orm.Query.get DEPRECATED: detected, successor wrong/absent
- A sqlalchemy.engine.RowProxy.keys REMOVED: detected, successor wrong/absent
- A successors correct 0/9
- A fp candidates (17): sqlalchemy.engine.result.ResultProxy.fetchall REMOVED; sqlalchemy.engine.result.ResultProxy.first REMOVED; sqlalchemy.engine.result.ResultProxy.scalar REMOVED; sqlalchemy.engine.result.RowProxy.__eq__ REMOVED; sqlalchemy.engine.result.RowProxy.keys REMOVED; sqlalchemy.ext.declarative.api.DeclarativeMeta.__init__ REMOVED; sqlalchemy.engine.base.Engine.connect SIGNATURE; sqlalchemy.engine.create_engine SIGNATURE
- B sqlalchemy.engine.result.RowProxy.keys: tier certain, calibration hit
- B calibration totals {'predicted': 59, 'hit': 41, 'prevented': 0, 'miss': 8, 'not_exercised': 10}; unpredicted changes []
- A BEHAVIOR entries left out of recall (verify's): 1, seen by the plan 0: ['sqlalchemy.engine.RowProxy']
- C tests after apply (equiv): 6 passed in 0.64s; attribution suite
- D equiv: fail (expected pass), counts {'changed': 1, 'rewritten': 24, 'disappeared': 6, 'appeared': 7, 'drift': 2, 'equal': 3}, missing changed [], detected by {}
- D change: fail (expected fail), counts {'changed': 1, 'rewritten': 24, 'disappeared': 7, 'appeared': 7, 'drift': 2, 'equal': 3}, missing changed [], detected by {'sqlalchemy.engine.result.RowProxy.keys': 'disappeared'}
- D levels {'exact': 8, 'type': 30}, opaque share 75%
- C expected rewrites 3, proposed 0
- F delta sides ['sqlalchemy new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/sqlalchemy/change/.pymolt/cache/envs/8e304b5439d46c2d)', 'sqlalchemy new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/sqlalchemy/equiv/.pymolt/cache/envs/8e304b5439d46c2d)', 'sqlalchemy old: baseline capture interpreter (bench/generalization/.envs/sqlalchemy/old/bin/python)']
- F inconclusive [], partial [], import errors []

### stdlib (python-stdlib, old python 3.8)
- status: ok
- A asyncio.sleep SIGNATURE: missed
- A asyncio.Queue SIGNATURE: missed
- A imp.reload MOVED: other_kind (plan kind REMOVED), successor wrong/absent
- A distutils.version.LooseVersion REMOVED: detected
- A collections.Mapping MOVED: detected, successor ok
- A unittest.TestCase.assertEquals REMOVED: missed, successor wrong/absent
- A inspect.getargspec REMOVED: detected, successor wrong/absent
- A inspect.ArgSpec.keywords REMOVED: missed, successor wrong/absent
- A xml.etree.ElementTree.Element.getchildren REMOVED: missed, successor wrong/absent
- A datetime.datetime.utcnow DEPRECATED: detected, successor wrong/absent
- A datetime.datetime.utcfromtimestamp DEPRECATED: detected, successor wrong/absent
- A successors correct 1/8
- B datetime.datetime.fromisoformat: tier None, calibration None
- B calibration totals {'predicted': 24, 'hit': 0, 'prevented': 0, 'miss': 0, 'not_exercised': 24}; unpredicted changes []
- C tests after apply (equiv): 10 passed, 1 warning in 0.03s; attribution suite
- D equiv: fail (expected pass), counts {'disappeared': 1, 'appeared': 1}, missing changed [], detected by {}
- D change: fail (expected fail), counts {'disappeared': 1, 'appeared': 1}, missing changed ['datetime.datetime.fromisoformat'], detected by {}
- D levels {'exact': 0, 'type': 0}, opaque share -
- E post-migration (new side) datetime.datetime.utcnow DeprecationWarning [C-level]: not observed
- E post-migration (new side) datetime.datetime.utcfromtimestamp DeprecationWarning [C-level]: not observed
- E post-migration (new side) re.findall (non-raw '\d+' literal) SyntaxWarning [C-level]: not observed
- E post-migration (new side) builtins.open (unclosed file) ResourceWarning [C-level]: not observed
- C expected rewrites 5, proposed 4
- F delta sides ['iniconfig new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/stdlib/change/.pymolt/cache/envs/67cd507464ddb3e4)', 'iniconfig new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/stdlib/equiv/.pymolt/cache/envs/67cd507464ddb3e4)', 'iniconfig old: baseline capture interpreter (bench/generalization/.envs/stdlib/old/bin/python)', 'packaging new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/stdlib/change/.pymolt/cache/envs/67cd507464ddb3e4)', 'packaging new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/stdlib/equiv/.pymolt/cache/envs/67cd507464ddb3e4)', 'packaging old: baseline capture interpreter (bench/generalization/.envs/stdlib/old/bin/python)', 'pluggy new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/stdlib/change/.pymolt/cache/envs/67cd507464ddb3e4)', 'pluggy new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/stdlib/equiv/.pymolt/cache/envs/67cd507464ddb3e4)', 'pluggy old: baseline capture interpreter (bench/generalization/.envs/stdlib/old/bin/python)', 'pytest new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/stdlib/change/.pymolt/cache/envs/67cd507464ddb3e4)', 'pytest new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/stdlib/equiv/.pymolt/cache/envs/67cd507464ddb3e4)', 'pytest old: baseline capture interpreter (bench/generalization/.envs/stdlib/old/bin/python)']
- E missed: ['asyncio.sleep DeprecationWarning', 'asyncio.Queue DeprecationWarning', 'imp DeprecationWarning', 'collections.Mapping DeprecationWarning', 'unittest.TestCase.assertEquals DeprecationWarning', 'inspect.getargspec DeprecationWarning', 'xml.etree.ElementTree.Element.getchildren DeprecationWarning', "re.findall (non-raw '\\d+' literal in app/xmlio.py) DeprecationWarning", 'builtins.open (unclosed file in xmlio.read_all) ResourceWarning']
- F inconclusive [], partial ['python'], import errors []
- runner warnings: ["manual_migration: app/reloader.py: old text not found: 'import imp\\nimport sys'", "manual_migration: app/reloader.py: old text not found: 'import imp\\nimport sys'"]

### urllib3 (http-client (offline), old python 3.11)
- status: ok
- A urllib3.util.retry.Retry SIGNATURE: detected, successor wrong/absent
- A urllib3.util.retry.Retry.DEFAULT_METHOD_WHITELIST REMOVED: detected, successor wrong/absent
- A urllib3.response.HTTPResponse.getheader DEPRECATED: detected, successor wrong/absent
- A urllib3.response.HTTPResponse.getheaders DEPRECATED: detected, successor wrong/absent
- A urllib3.get_host REMOVED: detected, successor ok
- A successors correct 1/5
- A fp candidates (8): urllib3.response.HTTPResponse SIGNATURE; urllib3.response.HTTPResponse.__init__ SIGNATURE; urllib3.util.retry.Retry.__init__ SIGNATURE; urllib3.response.HTTPResponse DEFAULT; urllib3.response.HTTPResponse.__init__ DEFAULT; urllib3.util.retry.Retry.__init__ DEFAULT; urllib3.util.timeout.Timeout DEFAULT; urllib3.util.timeout.Timeout.__init__ DEFAULT
- B urllib3.response.HTTPResponse.stream: tier None, calibration None
- B calibration totals {'predicted': 15, 'hit': 12, 'prevented': 0, 'miss': 3, 'not_exercised': 0}; unpredicted changes []
- C tests after apply (equiv): 7 passed in 0.05s; attribution suite
- D equiv: fail (expected pass), counts {'rewritten': 2, 'disappeared': 3, 'appeared': 1, 'drift': 2, 'equal': 5}, missing changed [], detected by {}
- D change: fail (expected fail), counts {'rewritten': 2, 'disappeared': 3, 'appeared': 1, 'drift': 3, 'equal': 7}, missing changed ['urllib3.response.HTTPResponse.stream'], detected by {}
- D levels {'exact': 7, 'type': 3}, opaque share 11%
- C expected rewrites 2, proposed 0
- F delta sides ['urllib3 new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/urllib3/change/.pymolt/cache/envs/090e85a53af8b8c4)', 'urllib3 new: throwaway environment from requirements-target.txt (bench/generalization/results/wave2/urllib3/equiv/.pymolt/cache/envs/090e85a53af8b8c4)', 'urllib3 old: baseline capture interpreter (bench/generalization/.envs/urllib3/old/bin/python)']
- F inconclusive [], partial [], import errors []

