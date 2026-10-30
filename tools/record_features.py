#!/usr/bin/env python3
"""Verify the finite 0.2 feature delivery and assemble local install artifacts.

No device execution, dependency download or private key/input publication.
Historical 0.1 manifests remain separate; snapshots and measurements are not RSS
or performance qualifications merely because a functional check passed.
"""
from pathlib import Path
import hashlib
import json
import re
import shutil
import zipfile

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'artifacts/features-20261030'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path):
    return path.resolve().relative_to(ROOT).as_posix()


def identity(path):
    return {'path':relative(path),'bytes':path.stat().st_size,'sha256':sha(path)}


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    if not __debug__:raise RuntimeError('identity assertions must be enabled')
    receipts=[]
    paths=[ROOT/'artifacts/functional-next-20261030/packed/receipt.json',
           BASE/'packed-art-build.json',BASE/'android-pressure-java-host-final.json',
           BASE/'android-pressure-build.json',BASE/'device/features-receipt.json',
           BASE/'device-android-final/features-receipt.json',BASE/'family-contracts.json']
    for path in paths:
        item=load(path)
        for source in item.get('sources',[]):
            assert sha(ROOT/source['path'])==source['sha256'],source['path']
        receipts.append(identity(path))
    packed,art,java,android,device,android_device,family=map(load,paths)
    assert packed['status']=='PASS' and packed['java_contracts']['cases']==32
    assert packed['real_kotlin_oracle']['policy_comparisons']==288 and not packed['BIL_modified']
    assert java['status']=='PASS' and java['checks']==38 and java['real_android_api_compile']
    assert art['status']=='PASS' and android['status']=='PASS'
    assert len(device['native_cases'])==9 and all(t['success'] for t in device['native_cases'])
    assert device['packed']['cases']==32 and device['packed']['status']=='PASS'
    assert android_device['state']=='PASS' and android_device['all_passed']
    app=android_device['android_report'];assert app['status']=='PASS' and app['checks']==20 and app['final_backing_bytes']==0
    assert not app['actual_low_memory_event'] and not app['performance_qualified']
    assert android_device['prior_native_ART_receipt_sha256']==sha(paths[4])
    assert sha(BASE/'validation-runner-executed-initial.txt')==device['source_sha256']
    assert android_device['source_sha256']==sha(ROOT/'tools/validate_features_device.py')
    for item in android['artifacts']:assert sha(ROOT/item['path'])==item['sha256']
    host=[]
    for profile in ('features-host','features-asan'):
        log=ROOT/'build'/profile/'Testing/Temporary/LastTest.log'
        text=log.read_text(encoding='utf-8')
        names=re.findall(r'^\d+/10 Testing: (.+)$',text,re.M)
        assert len(names)==10 and text.count('Test Passed.')==10 and 'Test Failed.' not in text
        cleaned=text.replace(str(ROOT),'<workspace>').replace(ROOT.as_posix(),'<workspace>')
        target=BASE/(profile+'-ctest-sanitized.log');target.write_text(cleaned,encoding='utf-8',newline='\n')
        cache=(ROOT/'build'/profile/'CMakeCache.txt').read_text(encoding='utf-8')
        sanitized='MGMEM_SANITIZE:BOOL=ON' in cache
        assert sanitized == (profile=='features-asan')
        binaries=[identity(ROOT/'build'/profile/name) for name in
          ['mgmem.dll','mgmem_other_runtime.dll','mgmem_core_tests.exe','mgmem_import_tests.exe',
           'mgmem_pressure_tests.exe','mgmem_cpp_consumer.exe','mgmem_abi10_consumer.exe','mgmem_runtime_tests.exe']]
        host.append({'profile':profile,'tests_passed':10,'test_names':names,'original_log_sha256':sha(log),
                     'public_log':identity(target),'address_sanitizer_enabled':sanitized,'binaries':binaries})
    dist=ROOT/'dist';dist.mkdir(exist_ok=True);packages=[]
    for name,prefix in [('Windows-x64',ROOT/'install/features-host'),('Android-arm64-v8a',ROOT/'install/features-arm64')]:
        target=dist/('MeguvizMemory-0.2.0-'+name+'.zip')
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(prefix.rglob('*')):
                if source.is_file():archive.write(source,source.relative_to(prefix).as_posix())
        with zipfile.ZipFile(target) as archive:assert archive.testzip() is None
        packages.append(identity(target))
    jar=ROOT/'build/packed-reader/meguviz-memory-packed-reader-0.1.0.jar'
    copied=dist/jar.name;shutil.copyfile(jar,copied);packages.append(identity(copied))
    source_aar=next(ROOT/i['path'] for i in android['artifacts'] if i['path'].endswith('.aar'))
    aar=dist/'MeguvizMemory-Android-Pressure-0.2.0.aar'
    with zipfile.ZipFile(source_aar) as source,zipfile.ZipFile(aar,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in source.namelist():archive.writestr(name,source.read(name))
        archive.write(ROOT/'third_party_notices/llvm-NOTICE.txt','META-INF/LICENSES/llvm-NOTICE.txt')
    with zipfile.ZipFile(aar) as archive,zipfile.ZipFile(source_aar) as source:
        assert all(archive.read(name)==source.read(name) for name in source.namelist())
    packages.append(identity(aar))
    lock=load(ROOT/'SOURCE_LOCK.json')
    for item in lock['files']:
        prefix=ROOT if item['root']=='MeguvizMemory' else ROOT/'build/locked-inputs/DexMeguviz'
        assert sha(prefix/item['path'])==item['sha256'],item['path']
    result={'schema_version':1,'version':'0.2.0','native_abi':'1.1','qualification':'FUNCTIONAL_ENGINEERING_PREVIEW_NOT_PERFORMANCE_OR_FORMAL_RELEASE',
      'source_identity':{'kind':'SCOPED_SOURCE_HASH_BUILD_NOT_DELIVERY_COMMIT','source_lock':identity(ROOT/'SOURCE_LOCK.json'),
                         'source_set_sha256':lock['source_set_sha256']},
      'receipts':receipts,'host':host,'device':{'profile':device['device'],'native_cases':9,'packed_ART_cases':32,
        'android_checks':20,'AMS_injected_callback':app['ams_injected_callback_status'],'actual_low_memory_event':False,
        'RSS_kB':app['rss_kb'],'PSS_kB':app['pss_kb'],'private_dirty_kB':app['private_dirty_kb'],'energy_joules':None,
        'native_ART_not_repeated_after_validator_fix':True},'packages':packages,
      'aar_notice_assembly':'Runtime/classes identical to built AAR; existing LLVM notice added for distribution',
      'BIL':'NOT_MODIFIED_NOT_INTEGRATED','family_bridge':'CONTRACT_AUDIT_ONLY_NOT_IMPLEMENTED',
      'remaining':['16KB runtime','MTE/HWASan','energy measurement','BIL host parseFrom/Hook/UI','family bridge runtime','performance qualification','formal release']}
    (BASE/'ARTIFACTS.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    (dist/'SHA256SUMS-0.2.0.txt').write_text(''.join(p['sha256']+'  '+Path(p['path']).name+'\n' for p in packages),encoding='ascii')
    print(json.dumps({'integrity':'PASS','packages':len(packages),'native_abi':'1.1','device_native':9,'packed_ART':32,'android':20}))


if __name__=='__main__':main()
