# Unix SMB/CIFS implementation. Tests for smb manipulation
# Copyright (C) David Mulder <dmulder@suse.com> 2018
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import errno
from samba import gpo, tests
from samba.gpclass import register_gp_extension, list_gp_extensions, \
    unregister_gp_extension, GPOStorage
from samba.param import LoadParm
from samba.gpclass import check_refresh_gpo_list, check_safe_path, \
    check_guid, parse_gpext_conf, atomic_write_conf, get_deleted_gpos_list
from subprocess import Popen, PIPE
from tempfile import NamedTemporaryFile, TemporaryDirectory
from samba.gp_sec_ext import gp_krb_ext, gp_access_ext
from samba.gp_scripts_ext import gp_scripts_ext
from samba.gp_sudoers_ext import gp_sudoers_ext
from samba.vgp_sudoers_ext import vgp_sudoers_ext
from samba.gpclass import gp_inf_ext
from samba.gp_smb_conf_ext import gp_smb_conf_ext
import logging
from samba.credentials import Credentials
from samba.gp_msgs_ext import gp_msgs_ext
from samba.common import get_bytes
from samba.dcerpc import preg
from samba.ndr import ndr_pack
import codecs
from shutil import copyfile
import xml.etree.ElementTree as etree

realm = os.environ.get('REALM')
policies = realm + '/POLICIES'
realm = realm.lower()
poldir = r'\\{0}\sysvol\{0}\Policies'.format(realm)
# the first part of the base DN varies by testenv. Work it out from the realm
base_dn = 'DC={0},DC=samba,DC=example,DC=com'.format(realm.split('.')[0])
dspath = 'CN=Policies,CN=System,' + base_dn
gpt_data = '[General]\nVersion=%d'

def days2rel_nttime(val):
    seconds = 60
    minutes = 60
    hours = 24
    sam_add = 10000000
    return -(val * seconds * minutes * hours * sam_add)

def gpupdate(lp, arg):
    gpupdate = lp.get('gpo update command')
    gpupdate.append(arg)

    p = Popen(gpupdate, stdout=PIPE, stderr=PIPE)
    stdoutdata, stderrdata = p.communicate()
    return p.returncode

def gpupdate_force(lp):
    return gpupdate(lp, '--force')

def gpupdate_unapply(lp):
    return gpupdate(lp, '--unapply')

def rsop(lp):
    return gpupdate(lp, '--rsop')

def stage_file(path, data):
    dirname = os.path.dirname(path)
    if not os.path.exists(dirname):
        try:
            os.makedirs(dirname)
        except OSError as e:
            if not (e.errno == errno.EEXIST and os.path.isdir(dirname)):
                return False
    if os.path.exists(path):
        os.rename(path, '%s.bak' % path)
    with NamedTemporaryFile(delete=False, dir=os.path.dirname(path)) as f:
        f.write(get_bytes(data))
        os.rename(f.name, path)
        os.chmod(path, 0o644)
    return True

def unstage_file(path):
    backup = '%s.bak' % path
    if os.path.exists(backup):
        os.rename(backup, path)
    elif os.path.exists(path):
        os.remove(path)

class GPOTests(tests.TestCase):
    def setUp(self):
        super(GPOTests, self).setUp()
        self.server = os.environ["SERVER"]
        self.dc_account = self.server.upper() + '$'
        self.lp = LoadParm()
        self.lp.load_default()
        self.creds = self.insta_creds(template=self.get_credentials())

    def tearDown(self):
        super(GPOTests, self).tearDown()

    def test_gpo_list(self):
        global poldir, dspath
        ads = gpo.ADS_STRUCT(self.server, self.lp, self.creds)
        if ads.connect():
            gpos = ads.get_gpo_list(self.creds.get_username())
        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        names = ['Local Policy', guid]
        file_sys_paths = [None, '%s\\%s' % (poldir, guid)]
        ds_paths = [None, 'CN=%s,%s' % (guid, dspath)]
        for i in range(0, len(gpos)):
            self.assertEqual(gpos[i].name, names[i],
                              'The gpo name did not match expected name %s' % gpos[i].name)
            self.assertEqual(gpos[i].file_sys_path, file_sys_paths[i],
                              'file_sys_path did not match expected %s' % gpos[i].file_sys_path)
            self.assertEqual(gpos[i].ds_path, ds_paths[i],
                              'ds_path did not match expected %s' % gpos[i].ds_path)

    def test_gpo_ads_does_not_segfault(self):
        try:
            ads = gpo.ADS_STRUCT(self.server, 42, self.creds)
        except:
            pass

    def test_gpt_version(self):
        global gpt_data
        local_path = self.lp.cache_path('gpo_cache')
        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        gpo_path = os.path.join(local_path, policies, guid)
        old_vers = gpo.gpo_get_sysvol_gpt_version(gpo_path)[1]

        with open(os.path.join(gpo_path, 'GPT.INI'), 'w') as gpt:
            gpt.write(gpt_data % 42)
        self.assertEqual(gpo.gpo_get_sysvol_gpt_version(gpo_path)[1], 42,
                          'gpo_get_sysvol_gpt_version() did not return the expected version')

        with open(os.path.join(gpo_path, 'GPT.INI'), 'w') as gpt:
            gpt.write(gpt_data % old_vers)
        self.assertEqual(gpo.gpo_get_sysvol_gpt_version(gpo_path)[1], old_vers,
                          'gpo_get_sysvol_gpt_version() did not return the expected version')

    def test_check_refresh_gpo_list(self):
        cache = self.lp.cache_path('gpo_cache')
        ads = gpo.ADS_STRUCT(self.server, self.lp, self.creds)
        if ads.connect():
            gpos = ads.get_gpo_list(self.creds.get_username())
        check_refresh_gpo_list(self.server, self.lp, self.creds, gpos)

        self.assertTrue(os.path.exists(cache),
                        'GPO cache %s was not created' % cache)

        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        gpt_ini = os.path.join(cache, policies,
                               guid, 'GPT.INI')
        self.assertTrue(os.path.exists(gpt_ini),
                        'GPT.INI was not cached for %s' % guid)

    def test_check_refresh_gpo_list_malicious_paths(self):
        # the path cannot contain ..
        path = '/usr/local/samba/var/locks/sysvol/../../../../../../root/'
        self.assertRaises(OSError, check_safe_path, path)

        self.assertEqual(check_safe_path('/etc/passwd'), 'etc/passwd')
        self.assertEqual(check_safe_path('\\\\etc/\\passwd'), 'etc/passwd')

        # there should be no backslashes used to delineate paths
        before = 'sysvol/' + realm + '\\Policies/' \
            '{31B2F340-016D-11D2-945F-00C04FB984F9}\\GPT.INI'
        after = realm + '/Policies/' \
            '{31B2F340-016D-11D2-945F-00C04FB984F9}/GPT.INI'
        result = check_safe_path(before)
        self.assertEqual(result, after, 'check_safe_path() didn\'t'
                          ' correctly convert \\ to /')

    def test_check_safe_path_typesafe_name(self):
        path = '\\\\toady.suse.de\\SysVol\\toady.suse.de\\Policies\\' \
               '{31B2F340-016D-11D2-945F-00C04FB984F9}\\GPT.INI'
        expected_path = 'toady.suse.de/Policies/' \
                        '{31B2F340-016D-11D2-945F-00C04FB984F9}/GPT.INI'

        result = check_safe_path(path)
        self.assertEqual(result, expected_path,
            'check_safe_path unable to detect variable case sysvol components')

    def test_gpt_ext_register(self):
        this_path = os.path.dirname(os.path.realpath(__file__))
        samba_path = os.path.realpath(os.path.join(this_path, '../../../'))
        ext_path = os.path.join(samba_path, 'python/samba/gp_sec_ext.py')
        ext_guid = '{827D319E-6EAC-11D2-A4EA-00C04F79F83A}'
        ret = register_gp_extension(ext_guid, 'gp_access_ext', ext_path,
                                    smb_conf=self.lp.configfile,
                                    machine=True, user=False)
        self.assertTrue(ret, 'Failed to register a gp ext')
        gp_exts = list_gp_extensions(self.lp.configfile)
        self.assertTrue(ext_guid in gp_exts.keys(),
                        'Failed to list gp exts')
        self.assertEqual(gp_exts[ext_guid]['DllName'], ext_path,
                          'Failed to list gp exts')

        unregister_gp_extension(ext_guid)
        gp_exts = list_gp_extensions(self.lp.configfile)
        self.assertTrue(ext_guid not in gp_exts.keys(),
                        'Failed to unregister gp exts')

        self.assertTrue(check_guid(ext_guid), 'Failed to parse valid guid')
        self.assertFalse(check_guid('AAAAAABBBBBBBCCC'), 'Parsed invalid guid')

        lp, parser = parse_gpext_conf(self.lp.configfile)
        self.assertTrue(lp and parser, 'parse_gpext_conf() invalid return')
        parser.add_section('test_section')
        parser.set('test_section', 'test_var', ext_guid)
        atomic_write_conf(lp, parser)

        lp, parser = parse_gpext_conf(self.lp.configfile)
        self.assertTrue('test_section' in parser.sections(),
                        'test_section not found in gpext.conf')
        self.assertEqual(parser.get('test_section', 'test_var'), ext_guid,
                          'Failed to find test variable in gpext.conf')
        parser.remove_section('test_section')
        atomic_write_conf(lp, parser)

    def test_gp_log_get_applied(self):
        local_path = self.lp.get('path', 'sysvol')
        guids = ['{31B2F340-016D-11D2-945F-00C04FB984F9}',
                 '{6AC1786C-016F-11D2-945F-00C04FB984F9}']
        gpofile = '%s/' + realm + '/Policies/%s/MACHINE/Microsoft/' \
                  'Windows NT/SecEdit/GptTmpl.inf'
        stage = '[System Access]\nMinimumPasswordAge = 998\n'
        cache_dir = self.lp.get('cache directory')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))
        for guid in guids:
            gpttmpl = gpofile % (local_path, guid)
            ret = stage_file(gpttmpl, stage)
            self.assertTrue(ret, 'Could not create the target %s' % gpttmpl)

        ret = gpupdate_force(self.lp)
        self.assertEqual(ret, 0, 'gpupdate force failed')

        gp_db = store.get_gplog(self.dc_account)

        applied_guids = gp_db.get_applied_guids()
        self.assertEqual(len(applied_guids), 2, 'The guids were not found')
        self.assertIn(guids[0], applied_guids,
                      '%s not in applied guids' % guids[0])
        self.assertIn(guids[1], applied_guids,
                      '%s not in applied guids' % guids[1])

        applied_settings = gp_db.get_applied_settings(applied_guids)
        for policy in applied_settings:
            self.assertIn('System Access', policy[1],
                          'System Access policies not set')
            self.assertIn('minPwdAge', policy[1]['System Access'],
                          'minPwdAge policy not set')
            if policy[0] == guids[0]:
                self.assertEqual(int(policy[1]['System Access']['minPwdAge']),
                                 days2rel_nttime(1),
                                 'minPwdAge policy not set')
            elif policy[0] == guids[1]:
                self.assertEqual(int(policy[1]['System Access']['minPwdAge']),
                                 days2rel_nttime(998),
                                 'minPwdAge policy not set')

        ads = gpo.ADS_STRUCT(self.server, self.lp, self.creds)
        if ads.connect():
            gpos = ads.get_gpo_list(self.dc_account)
        del_gpos = get_deleted_gpos_list(gp_db, gpos[:-1])
        self.assertEqual(len(del_gpos), 1, 'Returned delete gpos is incorrect')
        self.assertEqual(guids[-1], del_gpos[0][0],
                         'GUID for delete gpo is incorrect')
        self.assertIn('System Access', del_gpos[0][1],
                      'System Access policies not set for removal')
        self.assertIn('minPwdAge', del_gpos[0][1]['System Access'],
                      'minPwdAge policy not set for removal')

        for guid in guids:
            gpttmpl = gpofile % (local_path, guid)
            unstage_file(gpttmpl)

        ret = gpupdate_unapply(self.lp)
        self.assertEqual(ret, 0, 'gpupdate unapply failed')

    def test_process_group_policy(self):
        local_path = self.lp.cache_path('gpo_cache')
        guids = ['{31B2F340-016D-11D2-945F-00C04FB984F9}',
                 '{6AC1786C-016F-11D2-945F-00C04FB984F9}']
        gpofile = '%s/' + policies + '/%s/MACHINE/MICROSOFT/' \
                  'WINDOWS NT/SECEDIT/GPTTMPL.INF'
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        # Initialize the group policy extension
        ext = gp_krb_ext(logger, self.lp, machine_creds, store)

        ads = gpo.ADS_STRUCT(self.server, self.lp, machine_creds)
        if ads.connect():
            gpos = ads.get_gpo_list(machine_creds.get_username())

        stage = '[Kerberos Policy]\nMaxTicketAge = %d\n'
        opts = [100, 200]
        for i in range(0, 2):
            gpttmpl = gpofile % (local_path, guids[i])
            ret = stage_file(gpttmpl, stage % opts[i])
            self.assertTrue(ret, 'Could not create the target %s' % gpttmpl)

        # Process all gpos
        ext.process_group_policy([], gpos)

        ret = store.get_int('kdc:user_ticket_lifetime')
        self.assertEqual(ret, opts[1], 'Higher priority policy was not set')

        # Remove policy
        gp_db = store.get_gplog(machine_creds.get_username())
        del_gpos = get_deleted_gpos_list(gp_db, [])
        ext.process_group_policy(del_gpos, [])

        ret = store.get_int('kdc:user_ticket_lifetime')
        self.assertEqual(ret, None, 'MaxTicketAge should not have applied')

        # Process just the first gpo
        ext.process_group_policy([], gpos[:-1])

        ret = store.get_int('kdc:user_ticket_lifetime')
        self.assertEqual(ret, opts[0], 'Lower priority policy was not set')

        # Remove policy
        ext.process_group_policy(del_gpos, [])

        for guid in guids:
            gpttmpl = gpofile % (local_path, guid)
            unstage_file(gpttmpl)

    def test_gp_scripts(self):
        local_path = self.lp.cache_path('gpo_cache')
        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        reg_pol = os.path.join(local_path, policies, guid,
                               'MACHINE/REGISTRY.POL')
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        # Initialize the group policy extension
        ext = gp_scripts_ext(logger, self.lp, machine_creds, store)

        ads = gpo.ADS_STRUCT(self.server, self.lp, machine_creds)
        if ads.connect():
            gpos = ads.get_gpo_list(machine_creds.get_username())

        reg_key = b'Software\\Policies\\Samba\\Unix Settings'
        sections = { b'%s\\Daily Scripts' % reg_key : '.cron.daily',
                     b'%s\\Monthly Scripts' % reg_key : '.cron.monthly',
                     b'%s\\Weekly Scripts' % reg_key : '.cron.weekly',
                     b'%s\\Hourly Scripts' % reg_key : '.cron.hourly' }
        for keyname in sections.keys():
            # Stage the Registry.pol file with test data
            stage = preg.file()
            e = preg.entry()
            e.keyname = keyname
            e.valuename = b'Software\\Policies\\Samba\\Unix Settings'
            e.type = 1
            e.data = b'echo hello world'
            stage.num_entries = 1
            stage.entries = [e]
            ret = stage_file(reg_pol, ndr_pack(stage))
            self.assertTrue(ret, 'Could not create the target %s' % reg_pol)

            # Process all gpos, with temp output directory
            with TemporaryDirectory(sections[keyname]) as dname:
                ext.process_group_policy([], gpos, dname)
                scripts = os.listdir(dname)
                self.assertEquals(len(scripts), 1,
                    'The %s script was not created' % keyname.decode())
                out, _ = Popen([os.path.join(dname, scripts[0])], stdout=PIPE).communicate()
                self.assertIn(b'hello world', out,
                    '%s script execution failed' % keyname.decode())

                # Remove policy
                gp_db = store.get_gplog(machine_creds.get_username())
                del_gpos = get_deleted_gpos_list(gp_db, [])
                ext.process_group_policy(del_gpos, [])
                self.assertEquals(len(os.listdir(dname)), 0,
                                  'Unapply failed to cleanup scripts')

            # Unstage the Registry.pol file
            unstage_file(reg_pol)

    def test_gp_sudoers(self):
        local_path = self.lp.cache_path('gpo_cache')
        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        reg_pol = os.path.join(local_path, policies, guid,
                               'MACHINE/REGISTRY.POL')
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        # Initialize the group policy extension
        ext = gp_sudoers_ext(logger, self.lp, machine_creds, store)

        ads = gpo.ADS_STRUCT(self.server, self.lp, machine_creds)
        if ads.connect():
            gpos = ads.get_gpo_list(machine_creds.get_username())

        # Stage the Registry.pol file with test data
        stage = preg.file()
        e = preg.entry()
        e.keyname = b'Software\\Policies\\Samba\\Unix Settings\\Sudo Rights'
        e.valuename = b'Software\\Policies\\Samba\\Unix Settings'
        e.type = 1
        e.data = b'fakeu  ALL=(ALL) NOPASSWD: ALL'
        stage.num_entries = 1
        stage.entries = [e]
        ret = stage_file(reg_pol, ndr_pack(stage))
        self.assertTrue(ret, 'Could not create the target %s' % reg_pol)

        # Process all gpos, with temp output directory
        with TemporaryDirectory() as dname:
            ext.process_group_policy([], gpos, dname)
            sudoers = os.listdir(dname)
            self.assertEquals(len(sudoers), 1, 'The sudoer file was not created')
            self.assertIn(e.data,
                    open(os.path.join(dname, sudoers[0]), 'r').read(),
                    'The sudoers entry was not applied')

            # Remove policy
            gp_db = store.get_gplog(machine_creds.get_username())
            del_gpos = get_deleted_gpos_list(gp_db, [])
            ext.process_group_policy(del_gpos, [])
            self.assertEquals(len(os.listdir(dname)), 0,
                              'Unapply failed to cleanup scripts')

        # Unstage the Registry.pol file
        unstage_file(reg_pol)

    def test_vgp_sudoers(self):
        local_path = self.lp.cache_path('gpo_cache')
        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        manifest = os.path.join(local_path, policies, guid, 'MACHINE',
            'VGP/VTLA/SUDO/SUDOERSCONFIGURATION/MANIFEST.XML')
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        # Initialize the group policy extension
        ext = vgp_sudoers_ext(logger, self.lp, machine_creds, store)

        ads = gpo.ADS_STRUCT(self.server, self.lp, machine_creds)
        if ads.connect():
            gpos = ads.get_gpo_list(machine_creds.get_username())

        # Stage the manifest.xml file with test data
        stage = etree.Element('vgppolicy')
        policysetting = etree.Element('policysetting')
        stage.append(policysetting)
        version = etree.Element('version')
        version.text = '1'
        policysetting.append(version)
        data = etree.Element('data')
        sudoers_entry = etree.Element('sudoers_entry')
        command = etree.Element('command')
        command.text = 'ALL'
        sudoers_entry.append(command)
        user = etree.Element('user')
        user.text = 'ALL'
        sudoers_entry.append(user)
        principal_list = etree.Element('listelement')
        principal = etree.Element('principal')
        principal.text = 'fakeu'
        principal.attrib['type'] = 'user'
        principal_list.append(principal)
        sudoers_entry.append(principal_list)
        data.append(sudoers_entry)
        policysetting.append(data)
        ret = stage_file(manifest, etree.tostring(stage))
        self.assertTrue(ret, 'Could not create the target %s' % manifest)

        # Process all gpos, with temp output directory
        data = 'fakeu ALL=(ALL) NOPASSWD: ALL'
        with TemporaryDirectory() as dname:
            ext.process_group_policy([], gpos, dname)
            sudoers = os.listdir(dname)
            self.assertEquals(len(sudoers), 1, 'The sudoer file was not created')
            self.assertIn(data,
                    open(os.path.join(dname, sudoers[0]), 'r').read(),
                    'The sudoers entry was not applied')

            # Remove policy
            gp_db = store.get_gplog(machine_creds.get_username())
            del_gpos = get_deleted_gpos_list(gp_db, [])
            ext.process_group_policy(del_gpos, [])
            self.assertEquals(len(os.listdir(dname)), 0,
                              'Unapply failed to cleanup scripts')

        # Unstage the Registry.pol file
        unstage_file(manifest)

    def test_gp_inf_ext_utf(self):
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        ext = gp_inf_ext(logger, self.lp, machine_creds, store)
        test_data = '[Kerberos Policy]\nMaxTicketAge = 99\n'

        with NamedTemporaryFile() as f:
            with codecs.open(f.name, 'w', 'utf-16') as w:
                w.write(test_data)
            try:
                inf_conf = ext.read(f.name)
            except UnicodeDecodeError:
                self.fail('Failed to parse utf-16')
            self.assertIn('Kerberos Policy', inf_conf.keys(),
                          'Kerberos Policy was not read from the file')
            self.assertEquals(inf_conf.get('Kerberos Policy', 'MaxTicketAge'),
                              '99', 'MaxTicketAge was not read from the file')

        with NamedTemporaryFile() as f:
            with codecs.open(f.name, 'w', 'utf-8') as w:
                w.write(test_data)
            inf_conf = ext.read(f.name)
            self.assertIn('Kerberos Policy', inf_conf.keys(),
                          'Kerberos Policy was not read from the file')
            self.assertEquals(inf_conf.get('Kerberos Policy', 'MaxTicketAge'),
                              '99', 'MaxTicketAge was not read from the file')

    def test_rsop(self):
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        local_path = self.lp.cache_path('gpo_cache')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        ads = gpo.ADS_STRUCT(self.server, self.lp, machine_creds)
        if ads.connect():
            gpos = ads.get_gpo_list(machine_creds.get_username())

        gp_extensions = []
        gp_extensions.append(gp_krb_ext)
        gp_extensions.append(gp_scripts_ext)
        gp_extensions.append(gp_sudoers_ext)
        gp_extensions.append(gp_smb_conf_ext)
        gp_extensions.append(gp_msgs_ext)

        # Create registry stage data
        reg_pol = os.path.join(local_path, policies, '%s/MACHINE/REGISTRY.POL')
        reg_stage = preg.file()
        e = preg.entry()
        e.keyname = b'Software\\Policies\\Samba\\Unix Settings\\Daily Scripts'
        e.valuename = b'Software\\Policies\\Samba\\Unix Settings'
        e.type = 1
        e.data = b'echo hello world'
        e2 = preg.entry()
        e2.keyname = b'Software\\Policies\\Samba\\Unix Settings\\Sudo Rights'
        e2.valuename = b'Software\\Policies\\Samba\\Unix Settings'
        e2.type = 1
        e2.data = b'fakeu  ALL=(ALL) NOPASSWD: ALL'
        e3 = preg.entry()
        e3.keyname = 'Software\\Policies\\Samba\\smb_conf\\apply group policies'
        e3.type = 4
        e3.data = 1
        e3.valuename = 'apply group policies'
        e4 = preg.entry()
        e4.keyname = b'Software\\Policies\\Samba\\Unix Settings\\Messages'
        e4.valuename = b'issue'
        e4.type = 1
        e4.data = b'Welcome to \\s \\r \\l'
        reg_stage.num_entries = 4
        reg_stage.entries = [e, e2, e3, e4]

        # Create krb stage date
        gpofile = os.path.join(local_path, policies, '%s/MACHINE/MICROSOFT/' \
                  'WINDOWS NT/SECEDIT/GPTTMPL.INF')
        krb_stage = '[Kerberos Policy]\nMaxTicketAge = 99\n'

        for g in [g for g in gpos if g.file_sys_path]:
            ret = stage_file(gpofile % g.name, krb_stage)
            self.assertTrue(ret, 'Could not create the target %s' %
                                 (gpofile % g.name))
            ret = stage_file(reg_pol % g.name, ndr_pack(reg_stage))
            self.assertTrue(ret, 'Could not create the target %s' %
                                 (reg_pol % g.name))
            for ext in gp_extensions:
                ext = ext(logger, self.lp, machine_creds, store)
                ret = ext.rsop(g)
                self.assertEquals(len(ret.keys()), 1,
                                  'A single policy should have been displayed')

                # Check the Security Extension
                if type(ext) == gp_krb_ext:
                    self.assertIn('Kerberos Policy', ret.keys(),
                                  'Kerberos Policy not found')
                    self.assertIn('MaxTicketAge', ret['Kerberos Policy'],
                                  'MaxTicketAge setting not found')
                    self.assertEquals(ret['Kerberos Policy']['MaxTicketAge'], '99',
                                      'MaxTicketAge was not set to 99')
                # Check the Scripts Extension
                elif type(ext) == gp_scripts_ext:
                    self.assertIn('Daily Scripts', ret.keys(),
                                  'Daily Scripts not found')
                    self.assertIn('echo hello world', ret['Daily Scripts'],
                                  'Daily script was not created')
                # Check the Sudoers Extension
                elif type(ext) == gp_sudoers_ext:
                    self.assertIn('Sudo Rights', ret.keys(),
                                  'Sudoers not found')
                    self.assertIn('fakeu  ALL=(ALL) NOPASSWD: ALL',
                                  ret['Sudo Rights'],
                                  'Sudoers policy not created')
                # Check the smb.conf Extension
                elif type(ext) == gp_smb_conf_ext:
                    self.assertIn('smb.conf', ret.keys(),
                                  'apply group policies was not applied')
                    self.assertIn(e3.valuename, ret['smb.conf'],
                                  'apply group policies was not applied')
                    self.assertEquals(ret['smb.conf'][e3.valuename], e3.data,
                                      'apply group policies was not set')
                # Check the Messages Extension
                elif type(ext) == gp_msgs_ext:
                    self.assertIn('/etc/issue', ret,
                                  'Login Prompt Message not applied')
                    self.assertEquals(ret['/etc/issue'], e4.data,
                                      'Login Prompt Message not set')
            unstage_file(gpofile % g.name)
            unstage_file(reg_pol % g.name)

        # Check that a call to gpupdate --rsop also succeeds
        ret = rsop(self.lp)
        self.assertEquals(ret, 0, 'gpupdate --rsop failed!')

    def test_gp_unapply(self):
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        local_path = self.lp.cache_path('gpo_cache')
        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        ads = gpo.ADS_STRUCT(self.server, self.lp, machine_creds)
        if ads.connect():
            gpos = ads.get_gpo_list(machine_creds.get_username())

        gp_extensions = []
        gp_extensions.append(gp_krb_ext)
        gp_extensions.append(gp_scripts_ext)
        gp_extensions.append(gp_sudoers_ext)

        # Create registry stage data
        reg_pol = os.path.join(local_path, policies, '%s/MACHINE/REGISTRY.POL')
        reg_stage = preg.file()
        e = preg.entry()
        e.keyname = b'Software\\Policies\\Samba\\Unix Settings\\Daily Scripts'
        e.valuename = b'Software\\Policies\\Samba\\Unix Settings'
        e.type = 1
        e.data = b'echo hello world'
        e2 = preg.entry()
        e2.keyname = b'Software\\Policies\\Samba\\Unix Settings\\Sudo Rights'
        e2.valuename = b'Software\\Policies\\Samba\\Unix Settings'
        e2.type = 1
        e2.data = b'fakeu  ALL=(ALL) NOPASSWD: ALL'
        reg_stage.num_entries = 2
        reg_stage.entries = [e, e2]

        # Create krb stage date
        gpofile = os.path.join(local_path, policies, '%s/MACHINE/MICROSOFT/' \
                  'WINDOWS NT/SECEDIT/GPTTMPL.INF')
        krb_stage = '[Kerberos Policy]\nMaxTicketAge = 99\n'

        ret = stage_file(gpofile % guid, krb_stage)
        self.assertTrue(ret, 'Could not create the target %s' %
                             (gpofile % guid))
        ret = stage_file(reg_pol % guid, ndr_pack(reg_stage))
        self.assertTrue(ret, 'Could not create the target %s' %
                             (reg_pol % guid))

        # Process all gpos, with temp output directory
        remove = []
        with TemporaryDirectory() as dname:
            for ext in gp_extensions:
                ext = ext(logger, self.lp, machine_creds, store)
                if type(ext) == gp_krb_ext:
                    ext.process_group_policy([], gpos)
                    ret = store.get_int('kdc:user_ticket_lifetime')
                    self.assertEqual(ret, 99, 'Kerberos policy was not set')
                elif type(ext) in [gp_scripts_ext, gp_sudoers_ext]:
                    ext.process_group_policy([], gpos, dname)
                    gp_db = store.get_gplog(machine_creds.get_username())
                    applied_settings = gp_db.get_applied_settings([guid])
                    for _, fname in applied_settings[-1][-1][str(ext)].items():
                        self.assertIn(dname, fname,
                                      'Test file not created in tmp dir')
                        self.assertTrue(os.path.exists(fname),
                                        'Test file not created')
                        remove.append(fname)

            # Unapply policy, and ensure policies are removed
            gpupdate_unapply(self.lp)

            for fname in remove:
                self.assertFalse(os.path.exists(fname),
                                 'Unapply did not remove test file')
            ret = store.get_int('kdc:user_ticket_lifetime')
            self.assertNotEqual(ret, 99, 'Kerberos policy was not unapplied')

        unstage_file(gpofile % guid)
        unstage_file(reg_pol % guid)

    def test_smb_conf_ext(self):
        local_path = self.lp.cache_path('gpo_cache')
        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        reg_pol = os.path.join(local_path, policies, guid,
                               'MACHINE/REGISTRY.POL')
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        ads = gpo.ADS_STRUCT(self.server, self.lp, machine_creds)
        if ads.connect():
            gpos = ads.get_gpo_list(machine_creds.get_username())

        entries = []
        e = preg.entry()
        e.keyname = 'Software\\Policies\\Samba\\smb_conf\\template homedir'
        e.type = 1
        e.data = '/home/samba/%D/%U'
        e.valuename = 'template homedir'
        entries.append(e)
        e = preg.entry()
        e.keyname = 'Software\\Policies\\Samba\\smb_conf\\apply group policies'
        e.type = 4
        e.data = 1
        e.valuename = 'apply group policies'
        entries.append(e)
        e = preg.entry()
        e.keyname = 'Software\\Policies\\Samba\\smb_conf\\ldap timeout'
        e.type = 4
        e.data = 9999
        e.valuename = 'ldap timeout'
        entries.append(e)
        stage = preg.file()
        stage.num_entries = len(entries)
        stage.entries = entries

        ret = stage_file(reg_pol, ndr_pack(stage))
        self.assertTrue(ret, 'Failed to create the Registry.pol file')

        with NamedTemporaryFile(suffix='_smb.conf') as f:
            copyfile(self.lp.configfile, f.name)
            lp = LoadParm(f.name)

            # Initialize the group policy extension
            ext = gp_smb_conf_ext(logger, lp, machine_creds, store)
            ext.process_group_policy([], gpos)
            lp = LoadParm(f.name)

            template_homedir = lp.get('template homedir')
            self.assertEquals(template_homedir, '/home/samba/%D/%U',
                              'template homedir was not applied')
            apply_group_policies = lp.get('apply group policies')
            self.assertTrue(apply_group_policies,
                            'apply group policies was not applied')
            ldap_timeout = lp.get('ldap timeout')
            self.assertEquals(ldap_timeout, 9999, 'ldap timeout was not applied')

            # Remove policy
            gp_db = store.get_gplog(machine_creds.get_username())
            del_gpos = get_deleted_gpos_list(gp_db, [])
            ext.process_group_policy(del_gpos, [])

            lp = LoadParm(f.name)

            template_homedir = lp.get('template homedir')
            self.assertEquals(template_homedir, self.lp.get('template homedir'),
                              'template homedir was not unapplied')
            apply_group_policies = lp.get('apply group policies')
            self.assertEquals(apply_group_policies, self.lp.get('apply group policies'),
                              'apply group policies was not unapplied')
            ldap_timeout = lp.get('ldap timeout')
            self.assertEquals(ldap_timeout, self.lp.get('ldap timeout'),
                              'ldap timeout was not unapplied')

        # Unstage the Registry.pol file
        unstage_file(reg_pol)

    def test_gp_motd(self):
        local_path = self.lp.cache_path('gpo_cache')
        guid = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        reg_pol = os.path.join(local_path, policies, guid,
                               'MACHINE/REGISTRY.POL')
        logger = logging.getLogger('gpo_tests')
        cache_dir = self.lp.get('cache directory')
        store = GPOStorage(os.path.join(cache_dir, 'gpo.tdb'))

        machine_creds = Credentials()
        machine_creds.guess(self.lp)
        machine_creds.set_machine_account()

        # Initialize the group policy extension
        ext = gp_msgs_ext(logger, self.lp, machine_creds, store)

        ads = gpo.ADS_STRUCT(self.server, self.lp, machine_creds)
        if ads.connect():
            gpos = ads.get_gpo_list(machine_creds.get_username())

        # Stage the Registry.pol file with test data
        stage = preg.file()
        e1 = preg.entry()
        e1.keyname = b'Software\\Policies\\Samba\\Unix Settings\\Messages'
        e1.valuename = b'motd'
        e1.type = 1
        e1.data = b'Have a lot of fun!'
        stage.num_entries = 2
        e2 = preg.entry()
        e2.keyname = b'Software\\Policies\\Samba\\Unix Settings\\Messages'
        e2.valuename = b'issue'
        e2.type = 1
        e2.data = b'Welcome to \\s \\r \\l'
        stage.entries = [e1, e2]
        ret = stage_file(reg_pol, ndr_pack(stage))
        self.assertTrue(ret, 'Could not create the target %s' % reg_pol)

        # Process all gpos, with temp output directory
        with TemporaryDirectory() as dname:
            ext.process_group_policy([], gpos, dname)
            motd_file = os.path.join(dname, 'motd')
            self.assertTrue(os.path.exists(motd_file),
                            'Message of the day file not created')
            data = open(motd_file, 'r').read()
            self.assertEquals(data, e1.data, 'Message of the day not applied')
            issue_file = os.path.join(dname, 'issue')
            self.assertTrue(os.path.exists(issue_file),
                            'Login Prompt Message file not created')
            data = open(issue_file, 'r').read()
            self.assertEquals(data, e2.data, 'Login Prompt Message not applied')

            # Unapply policy, and ensure the test files are removed
            gp_db = store.get_gplog(machine_creds.get_username())
            del_gpos = get_deleted_gpos_list(gp_db, [])
            ext.process_group_policy(del_gpos, [], dname)
            data = open(motd_file, 'r').read()
            self.assertFalse(data, 'Message of the day file not removed')
            data = open(issue_file, 'r').read()
            self.assertFalse(data, 'Login Prompt Message file not removed')

        # Unstage the Registry.pol file
        unstage_file(reg_pol)
