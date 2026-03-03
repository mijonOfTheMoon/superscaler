Name:           superscaler
Version:        1.2.0
Release:        1%{?dist}
Summary:        Zero downtime supervisor worker autoscaler with pluggable queue backends
License:        MIT
Source0:        %{name}-%{version}.tar.gz
Source1:        superscaler.conf
Source2:        superscaler.service
BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
Requires:       python3
Requires:       python3-pip
AutoReqProv:    no

%description
Superscaler monitors queue backends and dynamically scales supervisor worker
process groups using a custom rpc plugin for zero downtime scaling.
Supports multiple queue backends simultaneously including RabbitMQ and Redis.

%prep
%autosetup

%build
python3 -m build --wheel --no-isolation

%install
pip3 install --root=%{buildroot} --no-deps --ignore-installed dist/*.whl

mkdir -p %{buildroot}%{_sysconfdir}/superscaler
mkdir -p %{buildroot}%{_unitdir}

install -m 644 %{SOURCE1} %{buildroot}%{_sysconfdir}/superscaler/superscaler.conf
install -m 644 %{SOURCE2} %{buildroot}%{_unitdir}/superscaler.service

%files
%license LICENSE
%{python3_sitelib}/superscaler/
%{python3_sitelib}/superscaler_plugin/
%{python3_sitelib}/superscaler-%{version}*.dist-info/
%{_bindir}/superscaler
%config(noreplace) %{_sysconfdir}/superscaler/superscaler.conf
%{_unitdir}/superscaler.service

%post
pip3 install 'redis>=4.0.0' 'pika>=1.2.0' 2>/dev/null || :
%systemd_post superscaler.service

%preun
%systemd_preun superscaler.service

%postun
if [ $1 -eq 0 ]; then
    # Full uninstall, not upgrade
    rm -rf %{_sysconfdir}/superscaler 2>/dev/null || :
    rmdir %{_localstatedir}/log/superscaler 2>/dev/null || :
fi
%systemd_postun superscaler.service

%changelog
* Mon Mar 03 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.2.0-1
- Refactor to pluggable multi-backend queue monitor abstraction
- Add RabbitMQ support via pika AMQP
- Replace single [redis] config with named [queue:*] backend sections
- Each target can now independently reference different queue backends
- Add queue backend reference param to target config

* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.10-1
- Remove redundant pending_timeout feature to rely on Supervisor stopwaitsecs and optimized zombie cleanup

* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.9-1
- Optimize zombie worker and pending scale down cleanup into a single RPC call per tick

* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.8-1
- Improve Redis connection error logging to catch and display all exceptions

* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.7-1
- Change default cooldown scaling values to 0 

* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.6-1
- Fix bug where scale up ignores pending scale down and exceeds max_workers limit

* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.5-1
- Make poll_interval parameter optional with default of 10

* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.4-1
- Rename configuration parameter group_name to program_name system-wide

* Fri Feb 27 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.3-1
- Make scaling bounds and cooldowns optional with default values
- Update target params in conf template and README

* Fri Feb 27 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.2-1
- Add version checking feature

* Thu Feb 27 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.1-1
- Fix documentation in entire codebase
- Change unix socket path to include unix:// in config file

* Wed Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.0-1
- Fix config update using line parser instead of regex
- Fix monotonic clock usage in main sleep loop
- Allow scale up during pending scale down operations
- Fix confirm scale down ordering to prevent state divergence
- Add pending timeout to clear stuck pending entries
- Remove defaults section, all target params now mandatory
- Remove http xml rpc support, unix socket only
- Add unix_socket_path config option
- Add pending_timeout config parameter
- Clean up rpm packaging and uninstall residue
- Remove log directory creation, output goes to journald

* Tue Feb 25 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.0.2-1
- Add pip3 install redis in post script
- Add python3-pip as runtime dependency

* Tue Feb 25 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.0.0-1
- Initial release