#!/usr/bin/env python3
"""
telegram_callback_validator.py - Validador de callback_data

Herramienta para:
1. Validar que todos los callback_data no exceden 64 bytes
2. Detectar callback_data duplicados
3. Generar reporte de seguridad
"""

import re
from typing import List, Tuple, Dict

class CallbackValidator:
    """Analiza y valida callback_data en tg_controller.py"""
    
    MAX_BYTES = 64  # Límite de Telegram
    
    def __init__(self):
        self.callbacks: List[Tuple[str, int, str]] = []  # (data, bytes, line)
        self.errors: List[str] = []
        self.warnings: List[str] = []
    
    def validate_file(self, filepath: str) -> bool:
        """
        Valida todos los callback_data en un archivo.
        
        Returns:
            True si no hay errores críticos, False si hay problemas graves
        """
        print(f"\n📋 Analizando {filepath}...\n")
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Buscar todos los callback_data
        pattern = r'callback_data=f?"([^"]+)"'
        matches = re.finditer(pattern, content)
        
        seen = {}
        for match in matches:
            callback_data = match.group(1)
            
            # Simular f-string si es necesario
            if '{' in callback_data:
                # Es un f-string, no podemos validar completamente
                # pero al menos advertimos
                self.warnings.append(
                    f"⚠️  F-string callback_data: {callback_data}\n"
                    f"   No se puede validar completamente. Asegúrate de que sea < 64 bytes."
                )
                continue
            
            # Contar bytes
            try:
                byte_size = len(callback_data.encode('utf-8'))
            except:
                byte_size = len(callback_data)
            
            self.callbacks.append((callback_data, byte_size, ""))
            
            # Validar tamaño
            if byte_size > self.MAX_BYTES:
                self.errors.append(
                    f"❌ CALLBACK_DATA DEMASIADO LARGO: {callback_data}\n"
                    f"   Tamaño: {byte_size} bytes (máximo: {self.MAX_BYTES})\n"
                    f"   Telegram rechazará este botón silenciosamente"
                )
            else:
                print(f"✅ {callback_data:40s} ({byte_size:2d} bytes)")
            
            # Detectar duplicados
            if callback_data in seen:
                self.warnings.append(
                    f"⚠️  CALLBACK_DATA DUPLICADO: {callback_data}\n"
                    f"   Aparece múltiples veces en el código"
                )
            else:
                seen[callback_data] = True
        
        return len(self.errors) == 0
    
    def print_report(self):
        """Genera reporte de validación"""
        
        print("\n" + "="*70)
        print("REPORTE DE VALIDACIÓN DE CALLBACK_DATA")
        print("="*70 + "\n")
        
        # Resumen
        print(f"📊 RESUMEN:")
        print(f"   Total de callbacks encontrados: {len(self.callbacks)}")
        print(f"   Errores críticos: {len(self.errors)}")
        print(f"   Advertencias: {len(self.warnings)}")
        
        # Errores
        if self.errors:
            print(f"\n❌ ERRORES CRÍTICOS ({len(self.errors)}):")
            for error in self.errors:
                print(f"   {error}\n")
        
        # Advertencias
        if self.warnings:
            print(f"\n⚠️  ADVERTENCIAS ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"   {warning}\n")
        
        # Estadísticas
        if self.callbacks:
            sizes = [b for _, b, _ in self.callbacks]
            print(f"\n📈 ESTADÍSTICAS:")
            print(f"   Tamaño promedio: {sum(sizes) / len(sizes):.1f} bytes")
            print(f"   Más pequeño: {min(sizes)} bytes")
            print(f"   Más grande: {max(sizes)} bytes")
            print(f"   Margen de seguridad: {self.MAX_BYTES - max(sizes)} bytes")
        
        print("\n" + "="*70)
        
        if self.errors:
            print("🚨 ACCIÓN REQUERIDA: Corrige los errores críticos")
            return False
        elif self.warnings:
            print("✅ No hay errores críticos, pero revisa las advertencias")
            return True
        else:
            print("🎉 TODO VÁLIDO - Todos los callbacks están bien configurados")
            return True


def validate_callback_patterns() -> Dict[str, List[str]]:
    """
    Valida patrones de callback en tg_controller.py
    Retorna dict con todos los patrones encontrados
    """
    
    patterns = {
        'close': [],
        'refresh': [],
        'stoplive': [],
        'notif': [],
        'other': []
    }
    
    try:
        with open('tg_controller.py', 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                if 'callback_data' in line:
                    if 'close:' in line:
                        patterns['close'].append(f"  Línea {line_num}: {line.strip()}")
                    elif 'refresh:' in line:
                        patterns['refresh'].append(f"  Línea {line_num}: {line.strip()}")
                    elif 'stoplive:' in line:
                        patterns['stoplive'].append(f"  Línea {line_num}: {line.strip()}")
                    elif 'notif:' in line:
                        patterns['notif'].append(f"  Línea {line_num}: {line.strip()}")
                    else:
                        patterns['other'].append(f"  Línea {line_num}: {line.strip()}")
    except FileNotFoundError:
        print("❌ No se encontró tg_controller.py")
        return {}
    
    return patterns


if __name__ == "__main__":
    import sys
    
    print("\n" + "🔍 VALIDADOR DE CALLBACK_DATA PARA TELEGRAM".center(70))
    print("="*70 + "\n")
    
    # Validar el archivo
    validator = CallbackValidator()
    success = validator.validate_file("tg_controller.py")
    
    # Mostrar reporte
    validator.print_report()
    
    # Mostrar patrones encontrados
    print("\n📌 PATRONES DE CALLBACK ENCONTRADOS:\n")
    patterns = validate_callback_patterns()
    for pattern_type, occurrences in patterns.items():
        if occurrences:
            print(f"  {pattern_type.upper()}:")
            for occ in occurrences[:3]:  # Mostrar máximo 3
                print(f"    {occ}")
            if len(occurrences) > 3:
                print(f"    ... y {len(occurrences) - 3} más")
            print()
    
    # Tips
    print("\n💡 TIPS DE SEGURIDAD:\n")
    print("  1. Mantén callback_data corto (< 50 bytes es ideal)")
    print("  2. Usa patrones consistentes (ej: 'action:param1:param2')")
    print("  3. Nunca exceeds 64 bytes, Telegram lo rechazará")
    print("  4. Los parámetros van separados por ':'")
    print("  5. Ejemplos válidos:")
    print("     • close:BTCUSDT (perfecto)")
    print("     • refresh:ETHUSDT (perfecto)")
    print("     • notif:trades (perfecto)")
    print("     • close:VERYLONGCOIN:WITH:MANY:PARAMETERS (probablemente mal)")
    
    sys.exit(0 if success else 1)
