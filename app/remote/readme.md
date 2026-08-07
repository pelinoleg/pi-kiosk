sudo cp /home/oleg/remote/remote-server.service /etc/systemd/system/                                                                                            
  sudo cp /home/oleg/remote/remote-listener.service /etc/systemd/system/                                                                                          
                                                                                                                                                                  
  # Перечитываем конфиг systemd                                                                                                                                   
  sudo systemctl daemon-reload

  # Включаем автозапуск
  sudo systemctl enable remote-server remote-listener

  # Запускаем сейчас
  sudo systemctl start remote-server
  sudo systemctl start remote-listener

  # Проверяем статус
  sudo systemctl status remote-server remote-listener

  Что делают сервисы:
  - remote-server — Flask на порту 5000, запускается от пользователя oleg, рестартится при падении
  - remote-listener — evdev слушатель, запускается от root (нужен для grab устройств), стартует только после сервера

  Полезные команды:
  - sudo systemctl restart remote-server remote-listener — перезапуск
  - sudo journalctl -u remote-server -f — логи сервера
  - sudo journalctl -u remote-listener -f — логи слушателя